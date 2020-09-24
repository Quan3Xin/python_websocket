#!/usr/bin/env python
# -*- coding:utf-8 -*-
import socket
import base64
import hashlib
import select
import traceback
import time
import multiprocessing
import gl

HOST = '127.0.0.1'
PORT = 26183
BUF_SIZE = 8096


def web_socket_init():
    '''
    初始化进程通信用的变量
    基于Manager对象创建IPC（Inter-Process Communication进程间通信）的消息载体dict、list、Queue
    :return:
    '''
    # 客户端消息队列dict: 保存不同客户端对应的消息队列
    gl.message_queues = gl.m.dict()
    # 客户端socket dict: 保存不同客户端对应的socket
    gl.client_socket_fd_map = gl.m.dict()
    # 统一接收消息队列：保存所有业务层发送过来的消息
    gl.ipc_queue = gl.m.Queue()


def select_process():
    '''
    启动2个子进程：websocket服务器和IPC消息队列
    :return:
    '''
    ps = multiprocessing.Process(target=start_socket_select_server,
                                 args=(gl.message_queues, gl.client_socket_fd_map))
    ps.start()

    pi = multiprocessing.Process(target=ipc_queue_receive,
                                 args=(gl.message_queues, gl.ipc_queue))
    pi.start()


def ipc_queue_receive(mq, ipc_q):
    '''
    统一接收业务层要发送的消息，分配到各客户端对应的消息队列中
    :param mq: 客户端消息队列dict
    :param ipc_q: 统一接收消息队列
    :return:
    '''
    gl.message_queues = mq
    gl.ipc_queue = ipc_q
    print('IPC Receive')
    while True:
        info = gl.ipc_queue.get(block=True)
        fd = int(info[0])
        msg = info[1]
        if fd in gl.message_queues.keys():
            gl.message_queues[fd].put(msg)


def start_socket_select_server(mq, client_socket_fd_map):
    '''
    websocket服务器
    :param mq: 客户端消息队列dict
    :param client_socket_fd_map: 客户端socket dict
    :return:
    '''
    # Manager对象本身是server服务，无法序列化，从而无法作为函数入参传递，必须在本子进程中另外创建
    # 用途：基于此Manager对象为新加入的客户端创建一个自己的消息队列
    m = multiprocessing.Manager()

    gl.message_queues = mq
    # select读监听列表
    gl.inputs = []
    # select写监听列表
    gl.outputs = []

    socketserver = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    socketserver.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    socketserver.bind((HOST, PORT))
    socketserver.listen(5)
    print('websocket 服务器启动成功，监听IP', (HOST, PORT))

    # 将socketserver放入select读监听列表
    gl.inputs.append(socketserver)

    # 初始化心跳检测使用变量
    gl.client_socket_fd_map = client_socket_fd_map
    gl.client_socket_heartbeat_map = {}
    heartbeat_check_time = time.clock()
    heartbeat_check_intervel_sec = 1

    while True:
        readable, writeable, exceptional = select.select(gl.inputs, gl.outputs, gl.inputs)
        # print('select finish, inputs size: %d, outputs size: %d' % (len(gl.inputs), len(gl.outputs)))
        for s in readable:
            print('readable:', s.fileno())
            if s is socketserver:
                # websocket 服务器接收客户端连接请求
                conn, address = socketserver.accept()
                print('new connection from:', address)
                # 将客户端socket放入select读监听列表
                gl.inputs.append(conn)
                # 为该客户端创建一个自己的消息队列
                q = m.Queue()
                gl.message_queues[conn.fileno()] = q
            else:
                if s not in gl.outputs and s in gl.inputs:
                    # 与新连接的客户端socket握手
                    data = s.recv(1024)
                    if data:
                        print('handshake from [%s]' % s.getpeername()[0])
                        headers = get_headers(data)
                        response_tpl = "HTTP/1.1 101 Switching Protocols\r\n" \
                                       "Upgrade:websocket\r\n" \
                                       "Connection:Upgrade\r\n" \
                                       "Sec-WebSocket-Accept:%s\r\n" \
                                       "WebSocket-Location:ws://%s%s\r\n\r\n"

                        magic_string = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
                        value = headers['Sec-WebSocket-Key'] + magic_string

                        ac = base64.b64encode(hashlib.sha1(value.encode('utf-8')).digest())
                        response_str = response_tpl % (ac.decode('utf-8'), headers['Host'], headers['url'])

                        s.send(bytes(response_str, encoding='utf-8'))

                        # 将客户端socket放入select写监听列表
                        if s not in gl.outputs:
                            print(s, type(s))
                            gl.outputs.append(s)
                            gl.client_socket_fd_map[s.fileno()] = s

                    # TCP连接：客户端主动关闭, 服务端会recv到0字节，反过来一样
                    else:
                        remove_connection(s.fileno(), gl.client_socket_fd_map)
                        print('1,客户端主动断开')
                else:
                    # websocket通信
                    try:
                        # 场景需求：服务器主动推送数据给客户端，客户端只需任意回复心跳检查即可，因此没有对recv的数据进行【解包】
                        info = s.recv(BUF_SIZE)
                    except Exception as e:
                        info = None
                    if info:
                        # 更新最后一次读记录，扣减心跳检查发送次数
                        if gl.client_socket_heartbeat_map[s.fileno()]['c'] > 0:
                            gl.client_socket_heartbeat_map[s.fileno()]['c'] -= 1

                    # TCP连接：客户端主动关闭, 服务端会recv到0字节，反过来一样
                    else:
                        remove_connection(s.fileno(), gl.client_socket_fd_map)
                        print('2,客户端主动断开')

        while True:
            write_doing_flag = True
            for s in writeable:
                w_fd = s.fileno()
                if w_fd not in gl.message_queues.keys():
                    continue
                if not gl.message_queues[w_fd].empty():
                    next_msg = gl.message_queues[w_fd].get_nowait()
                    send_ret = send_msg(s, next_msg)
                    print('send:', w_fd, next_msg)

                    if w_fd not in gl.client_socket_heartbeat_map.keys():
                        gl.client_socket_heartbeat_map[w_fd] = {}
                    if send_ret > 0:
                        # 更新客户端socket的写时间，重设心跳检查次数记录
                        gl.client_socket_heartbeat_map[w_fd]['w'] = time.process_time()
                        gl.client_socket_heartbeat_map[w_fd]['c'] = 0

                    write_doing_flag = False
            if write_doing_flag:
                break

        # 心跳检测：判断客户端是否异常断开
        cur = time.process_time()
        if cur - heartbeat_check_time > heartbeat_check_intervel_sec:
            heartbeat_check_time = cur
            tmp = gl.client_socket_heartbeat_map.copy()
            for k, v in tmp.items():
                write_delta = cur - v['w']
                count = v['c']

                # 超过10次未回应，则认为客户端异常断开，关闭该连接
                if count > 10:
                    print('k: %s, v: %s, cur: %s, write_delta: %s,' % (k, v, cur, write_delta))
                    remove_connection(k, gl.client_socket_fd_map)
                    print('心跳检测： 客户端[%s]超10次未回应，断开连接' % k)
                elif write_delta > heartbeat_check_intervel_sec:
                    # 发送心跳检查
                    pass
                    # msg = 'heart test'
                    #
                    # send_msg(gl.client_socket_fd_map[k], msg)
                    gl.client_socket_heartbeat_map[k]['c'] += 1
                    # print('k: %s, c:%d' % (k, gl.client_socket_heartbeat_map[k]['c']))


def remove_connection(fd, fd_map):
    '''
    停止对指定客户端的监听，删除相关关变量，关闭其socket连接
    :param fd: 要关闭客户端的文件描述符
    :param fd_map: 客户端socket dict
    :return:
    '''
    try:
        print('client [%s] closed' % fd)

        sock = str(fd_map[fd])
        print("------------------sock --- - %s " % sock)
        print("-------------------", gl.outputs)
        # print(gl.outputs, gl.message_queues, gl.client_socket_fd_map, gl.client_socket_heartbeat_map, gl.inputs)
        print("socket-------", str(fd_map[fd]), fd)
        print("-----------------", gl.message_queues)
        print("-----------------", gl.client_socket_fd_map)
        print("-----------------", gl.client_socket_heartbeat_map)
        gl.outputs.remove(sock)

        del gl.message_queues[fd]
        del gl.client_socket_fd_map[fd]
        del gl.client_socket_heartbeat_map[fd]

        gl.inputs.remove(sock)
        sock.close()
    except:
        sock.close()
        traceback.print_exc()



def get_headers(data):
    """
    将请求头格式化成字典
    :param data: 服务端接收到的数据，即conn.recv(8096)的返回值
    :return: 字典格式的请求头
    """
    header_dict = {}
    data = str(data, encoding='utf-8')

    header, body = data.split('\r\n\r\n', 1)
    header_list = header.split('\r\n')
    for i in range(0, len(header_list)):
        if i == 0:
            if len(header_list[i].split(' ')) == 3:
                header_dict['method'], header_dict['url'], header_dict['protocol'] = header_list[i].split(' ')
        else:
            k, v = header_list[i].split(':', 1)
            header_dict[k] = v.strip()
    return header_dict


def send_msg(conn, msg):
    """
    WebSocket服务端向客户端发送消息
    :param conn: 客户端连接到服务器端的socket对象,即： conn,address = socket.accept()
    :param msg_bytes: 向客户端发送的字节
    :return:
    """
    import struct

    msg_bytes = bytes(msg, encoding='utf-8')
    token = b"\x81"
    length = len(msg_bytes)
    if length < 126:
        token += struct.pack("B", length)
    elif length <= 0xFFFF:
        token += struct.pack("!BH", 126, length)
    else:
        token += struct.pack("!BQ", 127, length)

    msg = token + msg_bytes
    send_ret = conn.send(msg)
    return send_ret


def fd_send(fd, msg):
    '''
    业务层发消息的调用接口
    :param fd: 要发送消息的客户端socket对应的文件描述符
    :param msg: 消息内容
    :return:
    '''
    gl.ipc_queue.put((fd, msg))


if __name__ == '__main__':
    # 在主进程中创建Manager对象，启动一个server服务, 这个server是阻塞的, 以此来实现进程间数据安全.
    gl.m = multiprocessing.Manager()
    # 初始化需要进程通信的变量
    web_socket_init()
    # 启动2个子进程：websocket服务器和IPC消息队列
    select_process()
    # 注意：主进程不能到此结束，否则Manager对象控制的server服务将断开，无法进程通信
    # 此处通过while循环模拟web application server
    while True:
        # 模拟业务层发送数据
        for i in range(10):
            time.sleep(10)
            for fd in gl.client_socket_fd_map.keys():
                fd_send(fd, str(i))

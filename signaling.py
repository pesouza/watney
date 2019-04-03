#!/usr/bin/env python3
#
# Example 1-1 call signalling server
#
# Copyright (C) 2017 Centricular Ltd.
#
#  Author: Nirbheek Chauhan <nirbheek@centricular.com>
#

import os
import sys
import ssl
import logging
import asyncio
import websockets
from concurrent.futures._base import TimeoutError

ADDR_PORT = ('0.0.0.0', 8443)
KEEPALIVE_TIMEOUT = 10


class SignalingServer:
    def __init__(self):
        self.peers = dict()
        self.sessions = dict()
        self.rooms = dict()

    async def recv_msg_ping(self, ws, raddr):
        msg = None
        while msg is None:
            try:
                msg = await asyncio.wait_for(ws.recv(), KEEPALIVE_TIMEOUT)
            except TimeoutError:
                print('Sending keepalive ping to {!r} in recv'.format(raddr))
                await ws.ping()
        return msg

    async def disconnect(self, ws, peer_id):
        if peer_id in self.sessions:
            del self.sessions[peer_id]
        # Close connection
        if ws and ws.open:
            # Don't care about errors
            asyncio.ensure_future(ws.close(reason='hangup'))

    async def cleanup_session(self, uid):
        if uid in self.sessions:
            other_id = self.sessions[uid]
            del self.sessions[uid]
            print("Cleaned up {} session".format(uid))
            if other_id in self.sessions:
                del self.sessions[other_id]
                print("Also cleaned up {} session".format(other_id))
                # If there was a session with this peer, also
                # close the connection to reset its state.
                if other_id in self.peers:
                    print("Closing connection to {}".format(other_id))
                    wso, oaddr, _ = self.peers[other_id]
                    del self.peers[other_id]
                    await wso.close()

    async def cleanup_room(self, uid, room_id):
        room_peers = self.rooms[room_id]
        if uid not in room_peers:
            return
        room_peers.remove(uid)
        for pid in room_peers:
            wsp, paddr, _ = self.peers[pid]
            msg = 'ROOM_PEER_LEFT {}'.format(uid)
            print('room {}: {} -> {}: {}'.format(room_id, uid, pid, msg))
            await wsp.send(msg)

    async def remove_peer(self, uid):
        await self.cleanup_session(uid)
        if uid in self.peers:
            ws, raddr, status = self.peers[uid]
            if status and status != 'session':
                await self.cleanup_room(uid, status)
            del self.peers[uid]
            await ws.close()
            print("Disconnected from peer {!r} at {!r}".format(uid, raddr))

    async def connection_handler(self, ws, uid):
        raddr = ws.remote_address
        peer_status = None
        self.peers[uid] = [ws, raddr, peer_status]
        print("Registered peer {!r} at {!r}".format(uid, raddr))
        while True:
            # Receive command, wait forever if necessary
            msg = await self.recv_msg_ping(ws, raddr)
            # Update current status
            peer_status = self.peers[uid][2]
            # We are in a session or a room, messages must be relayed
            if peer_status is not None:
                # We're in a session, route message to connected peer
                if peer_status == 'session':
                    other_id = self.sessions[uid]
                    wso, oaddr, status = self.peers[other_id]
                    assert(status == 'session')
                    print("{} -> {}: {}".format(uid, other_id, msg))
                    await wso.send(msg)
                # We're in a room, accept room-specific commands
                elif peer_status:
                    # ROOM_PEER_MSG peer_id MSG
                    if msg.startswith('ROOM_PEER_MSG'):
                        _, other_id, msg = msg.split(maxsplit=2)
                        if other_id not in self.peers:
                            await ws.send('ERROR peer {!r} not found'
                                          ''.format(other_id))
                            continue
                        wso, oaddr, status = self.peers[other_id]
                        if status != room_id:
                            await ws.send('ERROR peer {!r} is not in the room'
                                          ''.format(other_id))
                            continue
                        msg = 'ROOM_PEER_MSG {} {}'.format(uid, msg)
                        print('room {}: {} -> {}: {}'.format(room_id, uid, other_id, msg))
                        await wso.send(msg)
                    elif msg == 'ROOM_PEER_LIST':
                        room_id = self.peers[peer_id][2]
                        room_peers = ' '.join([pid for pid in self.rooms[room_id] if pid != peer_id])
                        msg = 'ROOM_PEER_LIST {}'.format(room_peers)
                        print('room {}: -> {}: {}'.format(room_id, uid, msg))
                        await ws.send(msg)
                    else:
                        await ws.send('ERROR invalid msg, already in room')
                        continue
                else:
                    raise AssertionError('Unknown peer status {!r}'.format(peer_status))
            # Requested a session with a specific peer
            elif msg.startswith('SESSION'):
                print("{!r} command {!r}".format(uid, msg))
                _, callee_id = msg.split(maxsplit=1)
                if callee_id not in self.peers:
                    await ws.send('ERROR peer {!r} not found'.format(callee_id))
                    continue
                if peer_status is not None:
                    await ws.send('ERROR peer {!r} busy'.format(callee_id))
                    continue
                await ws.send('SESSION_OK')
                wsc = self.peers[callee_id][0]
                print('Session from {!r} ({!r}) to {!r} ({!r})'
                      ''.format(uid, raddr, callee_id, wsc.remote_address))
                # Register session
                self.peers[uid][2] = peer_status = 'session'
                self.sessions[uid] = callee_id
                self.peers[callee_id][2] = 'session'
                self.sessions[callee_id] = uid
            # Requested joining or creation of a room
            elif msg.startswith('ROOM'):
                print('{!r} command {!r}'.format(uid, msg))
                _, room_id = msg.split(maxsplit=1)
                # Room name cannot be 'session', empty, or contain whitespace
                if room_id == 'session' or room_id.split() != [room_id]:
                    await ws.send('ERROR invalid room id {!r}'.format(room_id))
                    continue
                if room_id in self.rooms:
                    if uid in self.rooms[room_id]:
                        raise AssertionError('How did we accept a ROOM command '
                                             'despite already being in a room?')
                else:
                    # Create room if required
                    self.rooms[room_id] = set()
                room_peers = ' '.join([pid for pid in self.rooms[room_id]])
                await ws.send('ROOM_OK {}'.format(room_peers))
                # Enter room
                self.peers[uid][2] = peer_status = room_id
                self.rooms[room_id].add(uid)
                for pid in self.rooms[room_id]:
                    if pid == uid:
                        continue
                    wsp, paddr, _ = self.peers[pid]
                    msg = 'ROOM_PEER_JOINED {}'.format(uid)
                    print('room {}: {} -> {}: {}'.format(room_id, uid, pid, msg))
                    await wsp.send(msg)
            else:
                print('Ignoring unknown message {!r} from {!r}'.format(msg, uid))

    async def hello_peer(self, ws):
        raddr = ws.remote_address
        hello = await ws.recv()
        hello, uid = hello.split(maxsplit=1)
        if hello != 'HELLO':
            await ws.close(code=1002, reason='invalid protocol')
            raise Exception("Invalid hello from {!r}".format(raddr))
        if not uid or uid in self.peers or uid.split() != [uid]: # no whitespace
            await ws.close(code=1002, reason='invalid peer uid')
            raise Exception("Invalid uid {!r} from {!r}".format(uid, raddr))
        # Send back a HELLO
        await ws.send('HELLO')
        return uid

    async def handler(self, ws, path):
        raddr = ws.remote_address
        print("Connected to {!r}".format(raddr))
        peer_id = await self.hello_peer(ws)
        try:
            await self.connection_handler(ws, peer_id)
        except websockets.ConnectionClosed:
            print("Connection to peer {!r} closed, exiting handler".format(raddr))
        finally:
            await self.remove_peer(peer_id)

    def start(self):
        # Create an SSL context to be used by the websocket server
        certpath = os.path.dirname(__file__)
        print('Using TLS with keys in {!r}'.format(certpath))
        chain_pem = os.path.join(certpath, 'cert.pem')
        key_pem = os.path.join(certpath, 'key.pem')
        sslctx = ssl.create_default_context()

        try:
            sslctx.load_cert_chain(chain_pem, keyfile=key_pem)
        except FileNotFoundError:
            print("Certificates not found, did you run generate_cert.sh?")
            sys.exit(1)
        # FIXME
        sslctx.check_hostname = False
        sslctx.verify_mode = ssl.CERT_NONE

        print("Listening on https://{}:{}".format(*ADDR_PORT))
        # Websocket server
        wsd = websockets.serve(self.handler, *ADDR_PORT, ssl=sslctx,
                               # Maximum number of messages that websockets will pop
                               # off the asyncio and OS buffers per connection. See:
                               # https://websockets.readthedocs.io/en/stable/api.html#websockets.protocol.WebSocketCommonProtocol
                               max_queue=16)

        logger = logging.getLogger('websockets.server')

        logger.setLevel(logging.ERROR)
        logger.addHandler(logging.StreamHandler())

        asyncio.get_event_loop().run_until_complete(wsd)

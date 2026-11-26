"""
Frost Bot (pure Python, no extra packages needed)

Joins a Minecraft Java server as a 1.8 client (protocol 47) in offline mode
and wanders around. Your server has ViaVersion + ViaBackwards + ViaRewind,
which lets old 1.8 clients join newer servers.
"""
import asyncio
import json
import math
import os
import random
import struct
import zlib

HOST = os.environ.get("MC_HOST", "FrostvaleSMP-FJRA.aternos.me")
PORT = int(os.environ.get("MC_PORT", "20309"))
USERNAME = os.environ.get("MC_USERNAME", "FrostBot")
RETRY = int(os.environ.get("RETRY_SECONDS", "30"))
PROTOCOL = 47  # Minecraft 1.8.x


# ---------- protocol helpers ----------
def vi(n):
    n &= 0xFFFFFFFF
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def rd_vi(buf, off):
    result, shift = 0, 0
    while True:
        b = buf[off]
        off += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            break
        shift += 7
        if shift > 35:
            raise ValueError("VarInt too big")
    if result & 0x80000000:
        result -= 1 << 32
    return result, off


async def read_vi(reader):
    result, shift = 0, 0
    while True:
        b = (await reader.readexactly(1))[0]
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result
        shift += 7
        if shift > 35:
            raise ValueError("VarInt too big")


def mc_str(s):
    b = s.encode("utf-8")
    return vi(len(b)) + b


def chat_text(raw):
    try:
        j = json.loads(raw)
        if isinstance(j, dict):
            parts = [j.get("text", "")] + [e.get("text", "") if isinstance(e, dict) else str(e) for e in j.get("extra", [])]
            return "".join(parts) or raw
        return str(j)
    except Exception:
        return raw


class Conn:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.threshold = -1
        self.lock = asyncio.Lock()

    async def send(self, pid, data=b""):
        body = vi(pid) + data
        if self.threshold >= 0:
            if len(body) >= self.threshold:
                payload = vi(len(body)) + zlib.compress(body)
            else:
                payload = vi(0) + body
        else:
            payload = body
        async with self.lock:
            self.writer.write(vi(len(payload)) + payload)
            await self.writer.drain()

    async def recv(self):
        length = await read_vi(self.reader)
        data = await self.reader.readexactly(length)
        if self.threshold >= 0:
            size, off = rd_vi(data, 0)
            data = data[off:] if size == 0 else zlib.decompress(data[off:])
        pid, off = rd_vi(data, 0)
        return pid, data[off:]

    def close(self):
        try:
            self.writer.close()
        except Exception:
            pass


# ---------- the bot ----------
class Bot:
    def __init__(self, conn):
        self.c = conn
        self.x = self.y = self.z = 0.0
        self.yaw = self.pitch = 0.0
        self.has_pos = False

    async def pos_look(self, on_ground=True):
        await self.c.send(0x06, struct.pack(">dddff?", self.x, self.y, self.z, self.yaw, self.pitch, on_ground))

    async def tick(self, on_ground=True):
        await self.pos_look(on_ground)
        await asyncio.sleep(0.05)

    async def wander(self):
        while not self.has_pos:
            await asyncio.sleep(0.2)
        while True:
            roll = random.random()
            if roll < 0.5:  # walk in a random direction
                self.yaw = random.uniform(0, 360)
                for _ in range(random.randint(40, 120)):
                    rad = math.radians(self.yaw)
                    self.x += -math.sin(rad) * 0.2
                    self.z += math.cos(rad) * 0.2
                    await self.tick(True)
            elif roll < 0.65:  # look around
                self.yaw = (self.yaw + random.uniform(-120, 120)) % 360
                self.pitch = max(-40.0, min(40.0, random.uniform(-30, 30)))
                await self.c.send(0x05, struct.pack(">ff?", self.yaw, self.pitch, True))
            elif roll < 0.8:  # small hop
                for dy in (0.4, 0.3, 0.2, 0.1, -0.1, -0.2, -0.3, -0.4):
                    self.y += dy
                    await self.tick(False)
                await self.tick(True)
            elif roll < 0.9:  # swing arm
                await self.c.send(0x0A)
            else:  # stand still
                for _ in range(random.randint(20, 60)):
                    await self.tick(True)
            for _ in range(random.randint(10, 80)):
                await self.tick(True)

    async def handle(self, pid, data):
        if pid == 0x00:  # keep alive
            kid, _ = rd_vi(data, 0)
            await self.c.send(0x00, vi(kid))
        elif pid == 0x08:  # server set our position
            x, y, z, yaw, pitch, flags = struct.unpack(">dddffB", data[:33])
            self.x = self.x + x if flags & 0x01 else x
            self.y = self.y + y if flags & 0x02 else y
            self.z = self.z + z if flags & 0x04 else z
            self.yaw = self.yaw + yaw if flags & 0x08 else yaw
            self.pitch = self.pitch + pitch if flags & 0x10 else pitch
            self.has_pos = True
            await self.pos_look(False)
        elif pid == 0x06:  # health update
            health = struct.unpack(">f", data[:4])[0]
            if health <= 0:
                print("[bot] Died, respawning...")
                await self.c.send(0x16, vi(0))
        elif pid == 0x40:  # kicked in play state
            raw, _ = rd_vi(data, 0)
            reason = data[_:_ + raw].decode("utf-8", "replace")
            raise ConnectionError("Kicked: " + chat_text(reason))


async def run_session(host, port, username):
    print(f"[bot] Connecting to {host}:{port} as {username}")
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 15)
    c = Conn(reader, writer)
    try:
        await c.send(0x00, vi(PROTOCOL) + mc_str(host) + struct.pack(">H", port) + vi(2))
        await c.send(0x00, mc_str(username))
        while True:  # login phase
            pid, data = await asyncio.wait_for(c.recv(), 30)
            if pid == 0x03:
                c.threshold, _ = rd_vi(data, 0)
            elif pid == 0x02:
                break
            elif pid == 0x01:
                raise ConnectionError("Server is in online-mode (set online-mode=false)")
            elif pid == 0x00:
                n, off = rd_vi(data, 0)
                raise ConnectionError("Login refused: " + chat_text(data[off:off + n].decode("utf-8", "replace")))
        print("[bot] Joined the server")
        bot = Bot(c)
        wander_task = asyncio.create_task(bot.wander())
        try:
            while True:
                pid, data = await asyncio.wait_for(c.recv(), 90)
                await bot.handle(pid, data)
                if wander_task.done():
                    wander_task.result()
        finally:
            wander_task.cancel()
    finally:
        c.close()


async def main():
    while True:
        try:
            await run_session(HOST, PORT, USERNAME)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[bot] Disconnected: {type(e).__name__}: {e}")
        print(f"[bot] Retrying in {RETRY}s...")
        await asyncio.sleep(RETRY)


if __name__ == "__main__":
    asyncio.run(main())

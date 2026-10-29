from types import SimpleNamespace

from app.services.realtime import ConnectionManager


class FakeSocket:
    def __init__(self, fails=False):
        self.fails = fails
        self.sent = []
        self.closed = False

    async def send_text(self, text):
        if self.fails:
            raise ConnectionError("gone")
        self.sent.append(text)

    async def close(self):
        self.closed = True


def make_manager():
    return ConnectionManager(SimpleNamespace(EXPOSE_WORKER_PID=False))


def test_capacity_check_leaves_no_empty_entries():
    manager = make_manager()
    assert manager.can_connect(1, "10.0.0.1")
    assert manager.rooms == {} and manager.ip_sockets == {}


async def test_broadcast_drops_only_the_dead_socket_everywhere():
    manager = make_manager()
    alive, dead = FakeSocket(), FakeSocket(fails=True)
    manager.connect(1, "10.0.0.1", alive)
    manager.connect(1, "10.0.0.2", dead)
    await manager.broadcast(1, "snapshot")
    assert alive.sent == ["snapshot"] and dead.closed
    assert manager.rooms[1] == {alive}
    assert manager.ip_sockets == {"10.0.0.1": {alive}}
    assert manager.socket_ips == {alive: "10.0.0.1"}
    # The socket's own handler still calls disconnect() afterwards; that must be harmless.
    manager.disconnect(1, "10.0.0.2", dead)
    manager.disconnect(1, "10.0.0.1", alive)
    assert manager.rooms == {} and manager.ip_sockets == {} and manager.socket_ips == {}

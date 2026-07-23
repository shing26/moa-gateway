import pytest

from app.channels.base import ChannelAdapter, ChannelMessage


class DummyChannel(ChannelAdapter):
    def __init__(self, results):
        self.results = results
        self.calls = []

    async def send(self, message: ChannelMessage) -> bool:
        self.calls.append(message)
        return bool(self.results.pop(0) if self.results else False)


@pytest.mark.asyncio
async def test_channel_adapter_dispatches():
    adapter = DummyChannel([True])
    message = ChannelMessage(channel="feishu", target="t1", text="hi", trace_id="trace-1")
    result = await adapter.send(message)
    assert result is True
    assert len(adapter.calls) == 1
    assert adapter.calls[0].channel == "feishu"

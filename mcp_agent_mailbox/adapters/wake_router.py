"""Select host-specific wake implementation without changing delivery rules."""
class WakeRouter:
    def __init__(self, channels):
        self.channels = {channel.host_type: channel for channel in channels if channel is not None}

    def for_host(self, host_type):
        return self.channels.get(host_type)

    def channel_status(self):
        return {host: channel.channel_status() for host, channel in self.channels.items()}

    def close(self):
        for channel in self.channels.values():
            close = getattr(channel, "close", None)
            if callable(close):
                close()

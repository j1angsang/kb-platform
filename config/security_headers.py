from __future__ import annotations


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app
        self.headers = (
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"same-origin"),  # same-origin 让同源带 Referer 保住防盗链，no-referrer 会让下载接口形同虚设
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                existing = {name.lower() for name, _ in message.get("headers", [])}
                for name, value in self.headers:
                    if name not in existing:
                        message.setdefault("headers", []).append((name, value))
            await send(message)

        await self.app(scope, receive, send_wrapper)

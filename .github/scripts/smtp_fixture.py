"""Loopback-only SMTP fixture; never relay, authenticate externally or send mail."""

from __future__ import annotations

import socketserver
import ssl
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class SMTPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, certificate: Path, key: Path, *, mode: str, refuse: bool = False):
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(certificate, key)
        self.mode, self.refuse = mode, refuse
        self.messages: list[bytes] = []
        self.received = threading.Condition()
        super().__init__(("127.0.0.1", 0), SMTPHandler)

    def get_request(self):
        connection, peer = super().get_request()
        connection.settimeout(5)
        if self.mode == "ssl":
            try:
                connection = self.context.wrap_socket(connection, server_side=True)
            except BaseException:
                connection.close()
                raise
        return connection, peer

    def handle_error(self, request, client_address):
        # Tests deliberately exercise rejected TLS handshakes/client timeouts.
        pass

    def wait_for_messages(self, count: int, timeout: float = 30) -> None:
        with self.received:
            if not self.received.wait_for(lambda: len(self.messages) >= count, timeout):
                raise AssertionError(
                    f"Expected {count} SMTP messages; received {len(self.messages)}"
                )


class SMTPHandler(socketserver.StreamRequestHandler):
    server: SMTPServer

    def reply(self, value: bytes) -> None:
        self.wfile.write(value + b"\r\n")
        self.wfile.flush()

    def handle(self) -> None:
        secure = self.server.mode == "ssl"
        self.reply(b"220 localhost test SMTP")
        while line := self.rfile.readline(65536):
            verb = line.split(b" ", 1)[0].strip().upper()
            if verb in (b"EHLO", b"HELO"):
                self.reply(b"250-localhost")
                if not secure and self.server.mode == "starttls":
                    self.reply(b"250-STARTTLS")
                self.reply(b"250 AUTH PLAIN" if secure else b"250 OK")
            elif verb == b"STARTTLS" and not secure and self.server.mode == "starttls":
                self.reply(b"220 ready for TLS")
                self.rfile.close()
                self.wfile.close()
                self.connection = self.server.context.wrap_socket(self.connection, server_side=True)
                self.request = self.connection
                self.rfile = self.connection.makefile("rb")
                self.wfile = self.connection.makefile("wb")
                secure = True
            elif verb == b"AUTH":
                self.reply(b"235 authenticated" if secure else b"530 TLS required")
            elif verb in (b"MAIL", b"RCPT", b"RSET"):
                self.reply(
                    b"550 deliberate refusal"
                    if verb == b"RCPT" and self.server.refuse
                    else b"250 OK"
                )
            elif verb == b"DATA" and secure:
                self.reply(b"354 end with dot")
                message = bytearray()
                while data := self.rfile.readline(65536):
                    if data == b".\r\n":
                        break
                    message.extend(data)
                with self.server.received:
                    self.server.messages.append(bytes(message))
                    self.server.received.notify_all()
                self.reply(b"250 accepted for fixture capture only")
            elif verb == b"QUIT":
                self.reply(b"221 bye")
                return
            else:
                self.reply(b"500 unsupported")


@contextmanager
def smtp_server(
    certificate: Path, key: Path, *, mode: str = "ssl", refuse: bool = False
) -> Iterator[SMTPServer]:
    with SMTPServer(certificate, key, mode=mode, refuse=refuse) as server:
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=10)

"""Fixed-host, bounded-time Alpaca transport without automatic order retries."""

from urllib.parse import urlsplit

from requests import Session


class PaperHTTPSession(Session):
    def __init__(self, *, host: str, read_only: bool):
        if host not in {"paper-api.alpaca.markets", "data.alpaca.markets"}:
            raise ValueError("unsupported Paper transport host")
        super().__init__()
        self.host = host
        self.read_only = read_only or host == "data.alpaca.markets"
        # Never inherit implicit proxy credentials or .netrc authentication.
        self.trust_env = False

    def request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        allowed = {"GET"} if self.read_only else {"GET", "POST", "DELETE"}
        if (method.upper() not in allowed or parsed.scheme != "https" or parsed.netloc != self.host
                or parsed.username or parsed.password or not parsed.path.startswith("/v2/")
                or parsed.fragment):
            raise RuntimeError("Paper transport rejected an unsupported request")
        kwargs["timeout"] = (3.05, 5.0)
        kwargs["allow_redirects"] = False
        return super().request(method, url, **kwargs)


def configure_transport(client, *, host: str, read_only: bool):
    """The installed SDK uses these fields; offline tests pin this integration.

    Disable its 429 retry loop too: an order intent must never wait and be
    automatically POSTed again with an old preflight. Uncertain outcomes are
    resolved by the existing original-client-ID lookup, never resubmission.
    """
    client._session.close()
    client._session = PaperHTTPSession(host=host, read_only=read_only)
    client._retry = 0

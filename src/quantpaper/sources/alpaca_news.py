"""Alpaca news metadata connector; copyrighted article bodies are not stored."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any

from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest

from ..alpaca_paper import PaperCredentials
from ..source_records import NewsRecord
from .common import FetchBatch, canonical_hash


class AlpacaNewsSource:
    def __init__(self, credentials: PaperCredentials, client: Any | None = None) -> None:
        self.client = client or NewsClient(credentials.key_id, credentials.secret_key)

    def fetch(self, symbols: list[str], days: int = 7, limit: int = 50) -> FetchBatch[NewsRecord]:
        if days < 1 or days > 365:
            raise ValueError("news lookback must be between 1 and 365 days")
        if limit < 1 or limit > 2500:
            raise ValueError("news limit must be between 1 and 2500")
        clean_symbols = sorted({symbol.strip().upper() for symbol in symbols if symbol.strip()})
        if not clean_symbols:
            raise ValueError("at least one news symbol is required")
        now = datetime.now(timezone.utc)
        request = NewsRequest(
            start=now - timedelta(days=days),
            end=now,
            symbols=",".join(clean_symbols),
            limit=limit,
            sort="desc",
            include_content=False,
        )
        response = self.client.get_news(request)
        # Availability is when the response was observed, not when the request
        # began. A slow/paginated fetch must not move first-seen time backwards.
        first_seen = datetime.now(timezone.utc)
        articles = [article for group in response.data.values() for article in group]
        records: list[NewsRecord] = []
        hash_payload: list[dict[str, object]] = []
        for article in articles:
            published = article.created_at.astimezone(timezone.utc)
            quality = {
                "updated_at": article.updated_at.astimezone(timezone.utc).isoformat(),
                "author": article.author,
                "historical_backfill": published < now - timedelta(days=1),
            }
            title_hash = hashlib.sha256(article.headline.encode("utf-8")).hexdigest()
            records.append(
                NewsRecord(
                    event_id=f"alpaca:{article.id}",
                    published_at=published.isoformat(),
                    first_seen_at=first_seen.isoformat(),
                    available_at=max(published, first_seen).isoformat(),
                    source=f"alpaca-news:{article.source}",
                    title_hash=title_hash,
                    raw_uri=article.url,
                    entity_ids=json.dumps(sorted(article.symbols)),
                    quality_flags=json.dumps(quality, sort_keys=True),
                )
            )
            hash_payload.append({"id": article.id, "headline_hash": title_hash, **quality})
        return FetchBatch(
            source="alpaca-news",
            request={"symbols": clean_symbols, "days": days, "limit": limit},
            records=records,
            content_hash=canonical_hash(hash_payload),
        )

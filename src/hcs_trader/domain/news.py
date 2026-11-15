"""Published news metadata with local observation time."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class NewsItem:
    source: str
    url: str
    title: str
    published_at: datetime
    received_at: datetime

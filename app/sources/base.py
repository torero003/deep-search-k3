from abc import ABC, abstractmethod
from typing import Optional


class SearchResult:
    __slots__ = ("title", "url", "content", "source", "score", "published_date", "raw_html", "engagement")

    def __init__(self, title: str, url: str, content: str, source: str,
                 score: float = 0.0, published_date: str = "", raw_html: str = "",
                 engagement: dict | None = None):
        self.title = title
        self.url = url
        self.content = content
        self.source = source
        self.score = score
        self.published_date = published_date
        self.raw_html = raw_html
        self.engagement = engagement

    def to_dict(self) -> dict:
        d = {
            "title": self.title,
            "url": self.url,
            "content": self.content,
            "source": self.source,
            "score": self.score,
            "published_date": self.published_date,
        }
        if self.engagement:
            d["engagement"] = self.engagement
        return d


class BaseSource(ABC):
    @abstractmethod
    def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """Search and return results."""
        ...

    @abstractmethod
    def health_check(self) -> dict:
        """Return health status: {available: bool, message: str}"""
        ...

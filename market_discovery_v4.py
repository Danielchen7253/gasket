import market_discovery_v3 as base


def search(client, query: str, per_query: int) -> list[dict]:
    rows = base.google_search(client, query, per_query, "web")
    if not rows:
        rows = base.fallback_search(client, query, per_query)
    seen = set()
    deduped = []
    for row in rows:
        key = row.get("url")
        if key and key not in seen:
            seen.add(key)
            row["image_url"] = None
            row["search_type"] = row.get("search_type") or "web"
            deduped.append(row)
    return deduped[:per_query * 2]


def main() -> None:
    base.search = search
    base.main()


if __name__ == "__main__":
    main()

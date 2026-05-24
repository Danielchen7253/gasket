import market_discovery_v5 as v5
import market_discovery_v3 as base

ORIGINAL_VALID_MODEL = base.valid_model


def valid_model(model: str) -> bool:
    upper = (model or "").upper()
    compact = base.normalize_model(model)
    if "/" in upper or "\\" in upper:
        return False
    if any(token in upper for token in ("HTTP", "WWW", ".COM", ".NET", ".ORG")):
        return False
    if any(token in compact for token in ("WWW", "COM", "HTTPS", "PRODUCTS", "REFRIGERATORS")):
        return False
    if upper.count("-") >= 3 and any(word in upper for word in ("SERIES", "PRODUCT", "MODEL")):
        return False
    return ORIGINAL_VALID_MODEL(model)


def main() -> None:
    base.valid_model = valid_model
    v5.main()


if __name__ == "__main__":
    main()

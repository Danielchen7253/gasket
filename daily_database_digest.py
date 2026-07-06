import os
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import httpx
import smtplib
from dotenv import load_dotenv


load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

MAIL_TO = os.getenv("DIGEST_EMAIL_TO", "danielchen7253@gmail.com")
MAIL_FROM = os.getenv("DIGEST_EMAIL_FROM", "Gasket Database <onboarding@resend.dev>")
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")


def supabase_headers(prefer: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Range": "0-0",
        "Range-Unit": "items",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def count_rows(client: httpx.Client, table: str, query: str = "") -> int:
    response = client.get(
        f"{SUPABASE_URL}/rest/v1/{table}?select=id{query}",
        headers=supabase_headers("count=exact"),
        timeout=30,
    )
    response.raise_for_status()
    return int(response.headers.get("content-range", "0-0/0").split("/")[-1])


def safe_count_rows(client: httpx.Client, table: str, query: str = "") -> int | None:
    try:
        return count_rows(client, table, query)
    except httpx.HTTPStatusError:
        return None


def safe_count_recent(
    client: httpx.Client,
    table: str,
    columns: list[str],
    since_iso: str,
) -> tuple[int | None, str | None]:
    for column in columns:
        value = safe_count_rows(client, table, f"&{column}=gte.{since_iso}")
        if value is not None:
            return value, column
    return None, None


def pct(part: int | None, total: int | None) -> str:
    if not part or not total:
        return "0%"
    return f"{round(part / total * 100, 1)}%"


def n(value: int | None) -> int:
    return value if value is not None else 0


def recent_line(label: str, value: int | None, column: str | None) -> str:
    if value is None:
        return f"- {label}：暂无可用时间字段统计"
    source = f"（按 {column}）" if column else ""
    return f"- {label}：{value}{source}"


def build_digest() -> tuple[str, str]:
    since_iso = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")

    with httpx.Client(timeout=30) as client:
        products_total = count_rows(client, "refrigerator_products")
        products_with_images = count_rows(client, "refrigerator_products", "&product_image_url=not.is.null")
        image_candidates_total = count_rows(client, "product_image_candidates")
        selected_image_candidates = safe_count_rows(client, "product_image_candidates", "&is_selected=eq.true")

        gasket_specs_total = count_rows(client, "product_gasket_specs")
        gasket_specs_candidate = count_rows(client, "product_gasket_specs", "&data_status=eq.candidate")
        gasket_specs_missing = safe_count_rows(client, "product_gasket_specs", "&data_status=eq.missing")
        gasket_specs_verified = safe_count_rows(client, "product_gasket_specs", "&is_verified=eq.true")

        gasket_details_total = count_rows(client, "gasket_details")
        verified_gasket_details = safe_count_rows(client, "gasket_details", "&is_verified=eq.true")
        gasket_parts_total = count_rows(client, "gasket_parts")

        recent_products, recent_products_column = safe_count_recent(
            client,
            "refrigerator_products",
            ["last_discovered_at", "last_enriched_at", "gasket_verified_at"],
            since_iso,
        )
        recent_images, recent_images_column = safe_count_recent(
            client,
            "product_image_candidates",
            ["created_at"],
            since_iso,
        )
        recent_gasket_details, recent_gasket_column = safe_count_recent(
            client,
            "gasket_details",
            ["scraped_at", "created_at"],
            since_iso,
        )
        recent_verified_events, recent_verified_column = safe_count_recent(
            client,
            "gasket_verification_events",
            ["created_at"],
            since_iso,
        )

    usable_verified = max(n(gasket_specs_verified), n(verified_gasket_details))
    missing_specs = n(gasket_specs_missing)

    today = datetime.now().strftime("%Y-%m-%d")
    subject = f"冰箱门封条数据库日报 - {today}"

    conclusion = []
    if products_total and products_with_images / products_total < 0.5:
        conclusion.append("产品主图覆盖率仍然偏低，图片补全应该继续优先跑。")
    if gasket_specs_total and gasket_specs_candidate / gasket_specs_total >= 0.95:
        conclusion.append("系统推荐密封条已经基本覆盖全部型号，verified 只用于安装成功后的确认标记。")
    elif gasket_specs_total and gasket_specs_candidate / gasket_specs_total >= 0.5:
        conclusion.append("系统推荐密封条覆盖已经过半，下一步重点是补齐剩余型号。")
    if not conclusion:
        conclusion.append("数据库在继续增长，今天重点看新增型号和可用资料是否同步增加。")

    body = "\n".join(
        [
            "冰箱门封条数据库日报",
            "",
            "一句话结论：",
            *[f"- {item}" for item in conclusion],
            "",
            "核心进度：",
            f"- 产品型号：{products_total}",
            f"- 产品主图：{products_with_images} / {products_total}（覆盖率 {pct(products_with_images, products_total)}）",
            f"- 系统推荐密封条：{gasket_specs_candidate} / {gasket_specs_total}（覆盖率 {pct(gasket_specs_candidate, gasket_specs_total)}）",
            f"- 已验证密封条：{usable_verified}",
            f"- 暂无推荐密封条：{missing_specs}",
            "",
            "过去 24 小时：",
            recent_line("新发现或更新产品型号", recent_products, recent_products_column),
            recent_line("新增产品图片候选", recent_images, recent_images_column),
            recent_line("新增密封条来源详情", recent_gasket_details, recent_gasket_column),
            recent_line("新增已验证安装记录", recent_verified_events, recent_verified_column),
            "",
            "后台候选池：",
            f"- 图片候选池：{image_candidates_total}（只代表待筛选图片，不代表已可用主图）",
            f"- 已选图片候选：{n(selected_image_candidates)}",
            f"- 密封条来源详情池：{gasket_details_total}",
            f"- 通用配件记录：{gasket_parts_total}",
            "",
            "下一步提醒：",
            "- 优先提升产品主图覆盖率，让查询页面看起来更完整。",
            "- 每个型号应优先拥有一个系统推荐密封条，并按交叉印证分数排序。",
            "- verified 只代表客户安装成功确认，不再作为写入密封条资料的前置条件。",
        ]
    )
    return subject, body


def send_with_resend(subject: str, body: str) -> None:
    if not RESEND_API_KEY:
        raise RuntimeError("Missing RESEND_API_KEY")
    response = httpx.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": MAIL_FROM,
            "to": [MAIL_TO],
            "subject": subject,
            "text": body,
        },
        timeout=30,
    )
    response.raise_for_status()


def send_with_smtp(subject: str, body: str) -> None:
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD]):
        raise RuntimeError("Missing SMTP settings")
    message = EmailMessage()
    message["From"] = MAIL_FROM
    message["To"] = MAIL_TO
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(message)


def main() -> None:
    subject, body = build_digest()
    if "--dry-run" in sys.argv or os.getenv("DIGEST_DRY_RUN") == "1":
        print(subject)
        print()
        print(body)
        return

    if RESEND_API_KEY:
        send_with_resend(subject, body)
    else:
        send_with_smtp(subject, body)
    print("daily database digest sent")


if __name__ == "__main__":
    main()

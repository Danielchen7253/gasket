import os
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import smtplib

import httpx
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


def safe_count_recent(client: httpx.Client, table: str, column: str = "created_at") -> int | None:
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    try:
        return count_rows(client, table, f"&{column}=gte.{since}")
    except httpx.HTTPStatusError:
        return None


def build_digest() -> tuple[str, str]:
    with httpx.Client(timeout=30) as client:
        products_total = count_rows(client, "refrigerator_products")
        products_with_images = count_rows(client, "refrigerator_products", "&product_image_url=not.is.null")
        image_candidates_total = count_rows(client, "product_image_candidates")
        gasket_specs_total = count_rows(client, "product_gasket_specs")
        gasket_specs_candidate = count_rows(client, "product_gasket_specs", "&data_status=eq.candidate")
        gasket_details_total = count_rows(client, "gasket_details")
        gasket_parts_total = count_rows(client, "gasket_parts")

        recent_products = safe_count_recent(client, "refrigerator_products")
        recent_images = safe_count_recent(client, "product_image_candidates")
        recent_gasket_details = safe_count_recent(client, "gasket_details")

    image_rate = round(products_with_images / products_total * 100, 1) if products_total else 0
    gasket_rate = round(gasket_specs_candidate / gasket_specs_total * 100, 1) if gasket_specs_total else 0
    today = datetime.now().strftime("%Y-%m-%d")
    subject = f"冰箱门封条数据库每日简讯 - {today}"

    def recent_line(label: str, value: int | None) -> str:
        if value is None:
            return f"- 过去 24 小时新增{label}：暂无法统计"
        return f"- 过去 24 小时新增{label}：{value}"

    body = "\n".join(
        [
            "冰箱门封条数据库每日简讯",
            "",
            "当前总量：",
            f"- 产品型号：{products_total}",
            f"- 已有产品图片的型号：{products_with_images}（覆盖率 {image_rate}%）",
            f"- 产品图片候选资料：{image_candidates_total}",
            f"- 密封条规格记录：{gasket_specs_total}",
            f"- candidate 状态密封条规格：{gasket_specs_candidate}（覆盖率 {gasket_rate}%）",
            f"- 密封条详情候选：{gasket_details_total}",
            f"- 通用配件记录：{gasket_parts_total}",
            "",
            "过去 24 小时：",
            recent_line("产品型号", recent_products),
            recent_line("图片候选", recent_images),
            recent_line("密封条候选", recent_gasket_details),
            "",
            "说明：candidate 代表已经找到候选资料，但还没有经过客户安装确认；verified 才代表 100% 确认。",
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
    if RESEND_API_KEY:
        send_with_resend(subject, body)
    else:
        send_with_smtp(subject, body)
    print("daily database digest sent")


if __name__ == "__main__":
    main()

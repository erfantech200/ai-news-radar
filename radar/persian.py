"""Persian helpers: digits, Jalali (Shamsi) dates, labels."""
from __future__ import annotations

from datetime import date

FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
JALALI_MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
                 "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
WEEKDAYS = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]
LANGUAGES = {
    "en": "انگلیسی", "zh": "چینی", "ja": "ژاپنی", "ko": "کره‌ای", "de": "آلمانی",
    "fr": "فرانسوی", "es": "اسپانیایی", "it": "ایتالیایی", "pt": "پرتغالی", "ru": "روسی",
    "ar": "عربی", "tr": "ترکی", "hi": "هندی", "fa": "فارسی", "nl": "هلندی", "he": "عبری",
}
CATEGORIES_FA = {
    "model_release": "مدل جدید", "product_launch": "محصول جدید", "research": "پژوهش",
    "open_source": "متن‌باز", "business_funding": "کسب‌وکار و سرمایه",
    "policy_regulation": "قانون‌گذاری", "hardware_chips": "سخت‌افزار و چیپ",
    "tools_for_creators": "ابزار کاربردی", "safety_ethics": "ایمنی و اخلاق",
    "big_tech": "غول‌های فناوری", "other_tech": "تکنولوژی",
}


def fa_digits(value) -> str:
    return str(value).translate(FA_DIGITS)


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = (355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100
            + (gy2 + 399) // 400 + gd + g_d_m[gm - 1])
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm, jd = 1 + days // 31, 1 + days % 31
    else:
        jm, jd = 7 + (days - 186) // 30, 1 + (days - 186) % 30
    return jy, jm, jd


def jalali_label(d: date) -> str:
    jy, jm, jd = gregorian_to_jalali(d.year, d.month, d.day)
    return f"{WEEKDAYS[d.weekday()]} {fa_digits(jd)} {JALALI_MONTHS[jm - 1]} {fa_digits(jy)}"


def language_name(code: str) -> str:
    return LANGUAGES.get((code or "").lower()[:2], code or "")


def category_name(code: str) -> str:
    return CATEGORIES_FA.get(code, code or "")


def edition_name(hour: int) -> str:
    """Which daily edition a run belongs to, by local hour."""
    if 4 <= hour < 11:
        return "صبح"
    if 11 <= hour < 17:
        return "ظهر"
    return "شب"

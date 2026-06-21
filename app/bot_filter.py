import re

# Known link-preview and crawler user-agent patterns
_PATTERNS = [
    r"WhatsApp",
    r"facebookexternalhit",
    r"Facebot",
    r"Slackbot",
    r"TelegramBot",
    r"Twitterbot",
    r"LinkedInBot",
    r"Discordbot",
    r"iMessagePreview",
    r"SkypeUriPreview",
    r"Applebot",
    r"Googlebot",
    r"bingbot",
    r"YandexBot",
    r"Baiduspider",
    r"DuckDuckBot",
    r"Viber",
    r"Line/",
    r"Pinterestbot",
    r"Redditbot",
    r"rogerbot",
    r"embedly",
    r"outbrain",
    r"quora link preview",
    r"Nuzzel",
    r"Bitrix link preview",
    r"XING-contenttabreceiver",
    r"Chrome-Lighthouse",
    r"PageSpeed",
    r"HeadlessChrome",
    r"Prerender",
    # Apple Mail Privacy Protection automated fetchers
    r"Apple-Mail-HTTPFetcher",
    r"Apple-Mail",
    r"AMS/\d",
    # Yahoo Mail link-scanner
    r"YahooMailProxy",
    # Outlook link-preview (safe-links prefetch)
    r"ms-outlook",
    r"Outlook-iOS-Android",
]

_BOT_RE = re.compile("|".join(_PATTERNS), re.IGNORECASE)


def is_preview_bot(user_agent: str | None) -> bool:
    if not user_agent:
        return False
    return bool(_BOT_RE.search(user_agent))

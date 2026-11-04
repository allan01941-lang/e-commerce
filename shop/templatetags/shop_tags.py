import bleach
from django import template
from django.utils.safestring import mark_safe
from bleach.css_sanitizer import CSSSanitizer
from urllib.parse import urlparse

register = template.Library()

DESCRIPTION_TAGS = [
    'p', 'br', 'strong', 'b', 'em', 'i', 'u', 's', 'del',
    'ul', 'ol', 'li', 'blockquote', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'hr', 'pre', 'code', 'div', 'span',
    'table', 'thead', 'tbody', 'tr', 'th', 'td',
    'figure', 'figcaption', 'img', 'oembed', 'iframe',
]
DESCRIPTION_ATTRIBUTES = {
    'a': {'href', 'title'},
    'ol': {'start', 'reversed'},
    'li': {'value'},
    'th': {'colspan', 'rowspan'},
    'td': {'colspan', 'rowspan'},
    'img': {'src', 'alt', 'width', 'height'},
    'oembed': {'url'},
    'iframe': {'src', 'title', 'width', 'height', 'allow', 'allowfullscreen', 'frameborder', 'loading', 'referrerpolicy'},
}
DESCRIPTION_CSS_SANITIZER = CSSSanitizer(allowed_css_properties=[
    'background-color',
    'border',
    'border-color',
    'border-style',
    'border-width',
    'color',
    'font-family',
    'font-size',
    'font-weight',
    'height',
    'max-width',
    'text-align',
    'width',
])


def allow_description_attribute(tag, name, value):
    if tag == 'iframe' and name == 'src':
        parsed_url = urlparse(value)
        return (
            parsed_url.scheme == 'https'
            and parsed_url.hostname in {
                'www.youtube.com',
                'www.youtube-nocookie.com',
                'player.vimeo.com',
            }
        )
    return name in {'class', 'style'} or name in DESCRIPTION_ATTRIBUTES.get(tag, set())


@register.filter
def safe_product_description(value):
    sanitized = bleach.clean(
        value or '',
        tags=DESCRIPTION_TAGS,
        attributes=allow_description_attribute,
        protocols=['http', 'https', 'mailto'],
        css_sanitizer=DESCRIPTION_CSS_SANITIZER,
        strip=True,
    )
    return mark_safe(sanitized)

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
FACEBOOK_CONVERSIONS_API_ACCESS_TOKEN = os.environ.get('FACEBOOK_CONVERSIONS_API_ACCESS_TOKEN', '')
FACEBOOK_GRAPH_API_VERSION = os.environ.get('FACEBOOK_GRAPH_API_VERSION', 'v26.0')

SECRET_KEY = 'fbj81lw(p(-!*ov6a3ds=%lxqq#d71e*6^k-0fw0f+w=*r)w)-'
DEBUG = True
ALLOWED_HOSTS = ['*']

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'shop',
    'django_ckeditor_5',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'ecommerce_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'shop.context_processors.cart_count',
                'shop.context_processors.site_settings',
                'shop.context_processors.seo_metadata',
                'shop.context_processors.staff_navigation',
            ],
        },
    },
]

WSGI_APPLICATION = 'ecommerce_project.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Dhaka'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

CKEDITOR_5_CONFIGS = {
    'default': {
        'toolbar': {
            'items': [
                'undo', 'redo', '|',
                'findAndReplace', 'selectAll', '|',
                'heading', 'style', '|',
                'fontSize', 'fontFamily', 'fontColor', 'fontBackgroundColor', 'highlight', '|',
                'bold', 'italic', 'underline', 'strikethrough', 'subscript', 'superscript', 'code', 'removeFormat', '|',
                'alignment', 'outdent', 'indent', '|',
                'link', 'insertImage', 'imageUpload', 'mediaEmbed', 'insertTable', 'horizontalLine', 'specialCharacters', '|',
                'bulletedList', 'numberedList', 'todoList', '|',
                'blockQuote', 'codeBlock', 'sourceEditing',
            ],
            'shouldNotGroupWhenFull': True,
        },
        'image': {
            'toolbar': [
                'imageTextAlternative', 'toggleImageCaption', '|',
                'imageStyle:inline', 'imageStyle:block', 'imageStyle:side', '|',
                'resizeImage',
            ],
        },
        'table': {
            'contentToolbar': [
                'tableColumn', 'tableRow', 'mergeTableCells',
                'tableProperties', 'tableCellProperties',
            ],
        },
        'mediaEmbed': {
            'previewsInData': True,
        },
        'heading': {
            'options': [
                {'model': 'paragraph', 'title': 'Paragraph', 'class': 'ck-heading_paragraph'},
                {'model': 'heading2', 'view': 'h2', 'title': 'Heading 2', 'class': 'ck-heading_heading2'},
                {'model': 'heading3', 'view': 'h3', 'title': 'Heading 3', 'class': 'ck-heading_heading3'},
            ],
        },
        'list': {
            'properties': {
                'styles': True,
                'startIndex': True,
                'reversed': True,
            },
        },
    },
}

CKEDITOR_5_FILE_UPLOAD_PERMISSION = 'staff'
CKEDITOR_5_UPLOAD_FILE_TYPES = [
    'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'tiff',
]

from wagtail.core.models import Page
from wagtail.search import index
from django.db import models


class BlogIndex(Page):
    pass


class BlogPage(Page):
    introduction = models.CharField(max_length=255)
    is_featured = models.BooleanField(default=False)

    search_fields = Page.search_fields + [
        index.SearchField("introduction"),
        index.FilterField("is_featured"),
    ]

import wagtail_factories
import factory

from tests.models import BlogIndex, BlogPage


class BlogIndexFactory(wagtail_factories.PageFactory):
    class Meta:
        model = BlogIndex


class BlogPageFactory(wagtail_factories.PageFactory):
    class Meta:
        model = BlogPage

    introduction = factory.Faker("sentence")

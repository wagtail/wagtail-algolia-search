from tests.models import BlogPage
from wagtail_algolia_search.backend import AlgoliaSearchBackend
from django.test import TestCase
from unittest import mock

from tests.factories import BlogIndexFactory, BlogPageFactory
from wagtail.core.models import Page


class TestAlgoliaSearchBackend(TestCase):
    def setUp(self):
        self.backend = AlgoliaSearchBackend(
            {
                "INDEX_NAME": "wagtail-algolia-search",
                "APPLICATION_ID": "fake-app-id",
                "ADMIN_API_KEY": "fake-admin-api-key",
                "INDEX_SETTINGS": {},
            }
        )

    @mock.patch("wagtail_algolia_search.backend.SearchClient")
    def test_page_is_indexed(self, mock_SearchClient):
        client = mock_SearchClient.create()
        index = client.init_index()

        # 1. Create new pages
        root_page = Page.get_first_root_node()
        blog_index = BlogIndexFactory(title="Blog", parent=root_page)
        blog_page = BlogPageFactory(title="My blog page", parent=blog_index)

        # 2. Index the pages
        self.backend.add(blog_index)
        self.backend.add(blog_page)

        # 3. Check that the correct data is sent to Algolia
        blog_index_doc = index.save_objects.call_args_list[0][0][0][0]
        blog_page_doc = index.save_objects.call_args_list[1][0][0][0]

        # Check index page
        self.assertEqual(blog_index_doc["objectID"], f"tests.BlogIndex:{blog_index.pk}")
        self.assertEqual(blog_index_doc["title"], blog_index.title)
        self.assertTrue(blog_index_doc["wagtail_managed"])

        # Check blog page
        self.assertEqual(blog_page_doc["objectID"], f"tests.BlogPage:{blog_page.pk}")
        self.assertEqual(blog_page_doc["title"], blog_page.title)
        self.assertTrue(blog_page_doc["wagtail_managed"])
        self.assertEqual(
            blog_page_doc["tests__BlogPage"]["introduction"],
            blog_page.introduction,
        )

    @mock.patch("wagtail_algolia_search.backend.SearchClient")
    def test_search(self, mock_SearchClient):
        client = mock_SearchClient.create()
        index = client.init_index()

        # 1. Create new pages
        root_page = Page.get_first_root_node()
        blog_index = BlogIndexFactory(title="Blog", parent=root_page)
        first_blog_page = BlogPageFactory(
            title="Apples and Strawberries", parent=blog_index
        )
        second_blog_page = BlogPageFactory(title="Orange and Lemons", parent=blog_index)

        # 2. Index the pages
        self.backend.add(first_blog_page)
        self.backend.add(second_blog_page)

        # 3. Search for the first page
        # Mock out result
        index.search.return_value = {
            "hits": [
                {
                    "objectID": f"tests.BlogPage:{first_blog_page.pk}",
                }
            ],
            "nbHits": 1,
            "page": 0,
            "nbPages": 1,
            "hitsPerPage": 10,
            "facets": {},
        }

        results = list(self.backend.search("Apples", BlogPage))

        # 4. Check that .search is called with the correct arguments
        index.search.assert_called_once_with(
            "Apples",
            {
                "filters": "wagtail_managed:true",
                "attributesToRetrieve": ["objectID"],
                "attributesToHighlight": [],
            },
        )

        # 5. Check that I get the expected results
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], first_blog_page)

    def test_filter_fields(self):
        # Check that FilterFields are set as filterableAttributes

        self.fail()

    @mock.patch("wagtail_algolia_search.backend.SearchClient")
    def test_filtering(self, mock_SearchClient):
        # Check that filtering works
        client = mock_SearchClient.create()
        index = client.init_index()

        # 1. Create pages
        root_page = Page.get_first_root_node()
        blog_index = BlogIndexFactory(title="Blog", parent=root_page)
        first_blog_page = BlogPageFactory(
            title="First blog page", is_featured=True, parent=blog_index
        )
        second_blog_page = BlogPageFactory(title="Second blog page", parent=blog_index)

        # 2. Filter and search for pages
        # Mock out result
        index.search.return_value = {
            "hits": [
                {
                    "objectID": f"tests.BlogPage:{first_blog_page.pk}",
                },
                {
                    "objectID": f"tests.BlogPage:{second_blog_page.pk}",
                },
            ],
            "nbHits": 2,
            "page": 0,
            "nbPages": 1,
            "hitsPerPage": 10,
            "facets": {},
        }
        queryset = BlogPage.objects.filter(is_featured=True)
        results = list(self.backend.search("blog", queryset))

        # 3. Check that the correct results are returned
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], first_blog_page)

    def test_field_types(self):
        # Create a test that checks all native field types and their Algolia representation
        self.fail()

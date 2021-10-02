from collections import defaultdict
from typing import Any, Dict, OrderedDict

from algoliasearch.search_client import SearchClient
from django.apps import apps
from django.conf import settings
from django.db import models
from django.db.models import Case, Count, Manager, When

from wagtail.core.models import Page
from wagtail.search.backends.base import (
    BaseSearchBackend,
    BaseSearchQueryCompiler,
    BaseSearchResults,
    FilterFieldError,
)
from wagtail.search.index import (
    AutocompleteField,
    FilterField,
    RelatedFields,
    SearchField,
)


class ObjectIndexer:
    """Transforms model instances to Algolia documents

    The final result looks something like this:

        {
            "objectID": "blog.BlogPage:42",
            "title": "My blog page",
            "blog__BlogPage": {
                "introduction": "My introduction"
            }
        }
    """

    def get_object_id(self, obj):
        """Generates an object ID for the given model instance"""
        return f"{obj._meta.app_label}.{obj._meta.object_name}:{obj.pk}"

    def get_document(self, obj):
        """Generates an Algolia document representation for a given model instance"""
        model = type(obj)

        doc: Dict[str, Any] = defaultdict(dict)
        doc["objectID"] = self.get_object_id(obj)
        doc["wagtail_managed"] = True

        for field in model.get_search_fields():
            for current_field, value in self.prepare_field(obj, field):
                field_defined_in = current_field.get_definition_model(obj)

                # Search fields defined on Wagtail's Page class will be at the root of the document
                if field_defined_in == Page:
                    doc[current_field.field_name] = value

                # Search fields defined on a subclass will be in a nested object
                else:
                    doc[f"{obj._meta.app_label}__{obj._meta.object_name}"][
                        current_field.field_name
                    ] = value

        return doc

    def prepare_field(self, obj, field, parent_field=None):
        value = field.get_value(obj)
        if isinstance(field, (SearchField, AutocompleteField)):
            # For pure text fields, just return the value
            yield (field, value)

        elif isinstance(field, FilterField):
            if isinstance(value, (models.Manager, models.QuerySet)):
                value = list(value.values_list("pk", flat=True))
            elif isinstance(value, models.Model):
                value = value.pk
            elif isinstance(value, (list, tuple)):
                value = [
                    item.pk if isinstance(item, models.Model) else item
                    for item in value
                ]

            yield (field, value)

        elif isinstance(field, RelatedFields):
            # Related fields needs more processing
            sub_obj = value
            if sub_obj is None:
                return

            if isinstance(sub_obj, Manager):
                # Related fields with many objects return a list
                sub_objs = sub_obj.all()

                yield (
                    field,
                    [
                        {
                            current_field.field_name: value
                            for sub_field in field.fields
                            for current_field, value in self.prepare_field(
                                sub_obj, sub_field
                            )
                        }
                        for sub_obj in sub_objs
                    ],
                )

            else:
                # Related fields with only one object return a dictionary
                if callable(sub_obj):
                    sub_obj = sub_obj()

                yield (
                    field,
                    {
                        current_field.field_name: value
                        for sub_field in field.fields
                        for current_field, value in self.prepare_field(
                            sub_obj, sub_field
                        )
                    },
                )


class AlgoliaSearchQueryCompiler(BaseSearchQueryCompiler):
    # # BaseSearchQueryCompiler from Wagtail raises a NotImplemented error
    # # if we're using a custom made search backend.
    # # This gets called in process_filter which needs the filter
    # # to be processed so we just return the value.
    # # 'value' is already the processed lookup so we just need to return that.
    # def _process_lookup(self, field, lookup, value):
    #     return value
    #
    # def _get_filters_from_where_node(self, where_node, check_only=False):
    #     # A function we need to override to prevent search from crashing when .check is called
    #     pass

    def get_query(self):
        return (
            self.query.query_string,
            {
                "filters": "wagtail_managed:true",
                "attributesToRetrieve": ["objectID"],
                "attributesToHighlight": [],
            },
        )


class AlgoliaSearchResults(BaseSearchResults):
    supports_facet = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.index = self.backend.get_index()
        self._search_cache = None

    def search_index(self):
        """Returns a queryset of result objects"""
        index = self.backend.get_index()

        args = self.query_compiler.get_query()

        return index.index.search(*args)

    def _do_search(self):
        if self._search_cache:
            return self._search_cache

        queryset = self.query_compiler.queryset
        model = queryset.model

        results = self.search_index()

        # Filter out results whose type don't belong in the queryset
        pks = []

        for hit in results["hits"]:
            try:
                result_model_name, pk = hit["objectID"].split(":")
                result_model = apps.get_model(result_model_name)
                if issubclass(result_model, model):
                    pks.append(pk)
            except ValueError:
                # Handle cases where the objectID format is not something we expect. E.g. if there are other documents
                # in the index that Wagtail doesn't handle.
                continue

        preserved = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(pks)])

        results = queryset.filter(pk__in=pks).order_by(preserved)

        self._search_cache = results
        self._results_cache = list(results)
        self._count_cache = len(self._results_cache)
        return results

    def _do_count(self):
        # This ensures that Algolia is only called once per query
        if self._count_cache is None:
            self._do_search()

        return self._count_cache

    def facet(self, field_name):
        # Get field
        field = self.query_compiler._get_filterable_field(field_name)
        if field is None:
            raise FilterFieldError(
                'Cannot facet search results with field "'
                + field_name
                + "\". Please add index.FilterField('"
                + field_name
                + "') to "
                + self.query_compiler.queryset.model.__name__
                + ".search_fields.",
                field_name=field_name,
            )

        query = self._do_search()
        results = (
            query.values(field_name).annotate(count=Count("pk")).order_by("-count")
        )

        return OrderedDict(
            [(result[field_name], result["count"]) for result in results]
        )


class AlgoliaIndex:
    def __init__(
        self, backend, index_name, application_id, admin_api_key, index_settings
    ):
        self.backend = backend
        self.name = index_name
        self.index_settings = index_settings
        self.indexer = ObjectIndexer()

        # Each language gets its own index
        self.client = SearchClient.create(application_id, admin_api_key)
        self.index = self.client.init_index(index_name)

    def update_settings(self):
        """Update index settings on Algolia

        This is called when `update_index` is run.
        """

        default = self.index_settings["default"]

        for lang, _ in settings.WAGTAIL_CONTENT_LANGUAGES:
            index_settings = {**default, **self.index_settings.get(lang, {})}
            if not index_settings.get("attributesForFaceting"):
                index_settings["attributesForFaceting"] = []

            # Note: All Wagtail Search managed documents have the wagtail_managed attribute set to `true`.
            # This is used to filter out results when searching through Wagtail to only return Wagtail managed
            # documents.
            index_settings["attributesForFaceting"].append(
                "filterOnly(wagtail_managed)"
            )

            self.indices[lang].set_settings(index_settings)

    def add_model(self, model):
        # Not needed
        pass

    def add_item(self, obj):
        self.add_items(obj._meta.model, [obj])

    def add_items(self, model, objs):
        self.index.save_objects(
            [
                self.indexer.get_document(obj)
                for obj in objs
                # Do not index the root page because we don't want it to appear in search results
                if not isinstance(obj, Page) or not obj.is_root()
            ]
        )

    def delete_item(self, obj):
        object_id = self.indexer.get_object_id(obj)

        self.index.delete_object(object_id)


class AlgoliaSearchRebuilder:
    def __init__(self, index):
        self.index = index

    def start(self):
        self.index.update_settings()
        return self.index

    def finish(self):
        pass


class AlgoliaSearchBackend(BaseSearchBackend):
    query_compiler_class = AlgoliaSearchQueryCompiler
    results_class = AlgoliaSearchResults
    index_class = AlgoliaIndex
    rebuilder_class = AlgoliaSearchRebuilder

    def __init__(self, params):
        self.index_name = params.pop("INDEX_NAME")
        self.application_id = params.pop("APPLICATION_ID")
        self.admin_api_key = params.pop("ADMIN_API_KEY")
        self.index_settings = params.pop("INDEX_SETTINGS")

    def add_type(self, model):
        # Not needed
        pass

    def get_index(self):
        return self.index_class(
            self,
            self.index_name,
            self.application_id,
            self.admin_api_key,
            self.index_settings,
        )

    def get_index_for_model(self, model):
        """Returns an index appropriate for the given model"""
        return self.get_index()

    def get_rebuilder(self):
        return self.rebuilder_class(self.get_index())

    def add(self, obj):
        self.get_index().add_item(obj)

    def add_bulk(self, model, obj_list):
        self.get_index().add_items(model, obj_list)

    def delete(self, obj):
        self.get_index().delete_item(obj)

    def refresh_index(self):
        pass

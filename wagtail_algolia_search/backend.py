from collections import defaultdict
from typing import Any, Dict, OrderedDict

from algoliasearch.search_client import SearchClient
from django.apps import apps
from django.db import models
from django.db.models import Case, Count, Manager, When
from wagtail.search.index import get_indexed_models

from wagtail.core.models import Page, TranslatableMixin
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

    def get_field_key(self, model, search_field):
        field_defined_in = search_field.get_definition_model(model)
        return (
            f"{field_defined_in._meta.app_label}__{field_defined_in._meta.object_name}"
        )

    def get_document(self, obj):
        """Generates an Algolia document representation for a given model instance"""
        model = type(obj)

        doc: Dict[str, Any] = defaultdict(dict)
        doc["objectID"] = self.get_object_id(obj)

        # All Algolia documents with `wagtail_managed == True` was indexed by Wagtail.
        doc["wagtail_managed"] = True

        # Set locale as a root level attribute for faceting
        doc["locale"] = None
        if isinstance(obj, TranslatableMixin) and obj.locale:
            doc["locale"] = obj.locale.language_code

        # `model` is in the format app_label.ModelName and is used for faceting
        doc["model"] = f"{obj._meta.app_label}.{obj._meta.object_name}"

        for field in model.get_search_fields():
            for current_field, value in self.prepare_field(obj, field):
                doc[self.get_field_key(model, current_field)][
                    current_field.field_name
                ] = value

        return doc

    def prepare_field(self, obj, field, parent_field=None):
        """Yields the prepared value for a given SearchField/FilterField"""
        value = field.get_value(obj)
        if isinstance(field, (SearchField, AutocompleteField)):
            # For pure text fields, just return the value
            yield (field, value)

        elif isinstance(field, FilterField):
            # FilterFields can be on a number of field types
            if isinstance(value, (models.Manager, models.QuerySet)):
                # For ManyToMany fields, return the list of related PKs
                value = list(value.values_list("pk", flat=True))

            elif isinstance(value, models.Model):
                # For ForeignKeys, return the PK of the related object
                value = value.pk

            elif isinstance(value, (list, tuple)):
                # If the value is a list (e.g. returned by a model method), return the list of PKs if the items are
                # model instances. Otherwise, just return the items raw and trust that the developer has returned a list
                # of serializable objects.
                value = [
                    item.pk if isinstance(item, models.Model) else item
                    for item in value
                ]

            # If none of the above matches, then just return the value and cross our fingers that it's JSON
            # serializable.
            yield (field, value)

        elif isinstance(field, RelatedFields):
            # Handle RelatedFields
            sub_obj = value
            if sub_obj is None:
                # If the sub object does not exist, set its value to None
                yield (field, None)

            if isinstance(sub_obj, Manager):
                # If the RelatedField is a ManyToMany, return a list of sub objects
                # For example:
                # RelatedFields('books', [
                #     index.SearchField('name'),
                #     index.FilterField('year_published'),
                # ])
                #
                # Returns:
                # "author": [
                #     {
                #         'name': 'The Chronicles of Narnia',
                #         'year_published': '1950'
                #     },
                #     {
                #         'name': 'Project Hail Mary',
                #         'year_published': '2021'
                #     }
                # ]
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
                # If the RelatedField is a ForeignKey, return a dictionary
                # For example:
                # RelatedFields('author', [
                #     index.SearchField('name'),
                #     index.FilterField('date_of_birth'),
                # ])
                #
                # Returns:
                # "author": {
                #     'name': 'John Doe',
                #     'date_of_birth': '1989-02-05'
                # }
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
    def _get_filters_from_where_node(self, where_node, check_only=False):
        # Filtering is done on the database so this method is unnecessary.
        pass

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

        index_settings = self.index_settings.copy()
        if not index_settings.get("attributesForFaceting"):
            index_settings["attributesForFaceting"] = []

        index_settings["attributesForFaceting"] += [
            # All Wagtail Search managed documents have the wagtail_managed attribute set to `true`.
            # This is used to filter out results when searching through Wagtail to only return Wagtail managed
            # documents.
            "filterOnly(wagtail_managed)",
            # Allow filtering and faceting by locale language code
            "locale",
            # Allow filtering and faceting by model type
            "model",
        ]

        # Add all FilterFields to attributesForFaceting
        # NB: While we might be adding FilterFields to attributesForFaceting, we don't actually use the filters when
        # searching. This functionality is here so the developer can choose to filter by FilterFields when querying
        # Algolia directly. For example, when querying Algolia on the front-end.
        for model in get_indexed_models():
            for filter_field in model.get_filterable_search_fields():
                if filter_field.get_definition_model(model) == model:
                    index_settings["attributesForFaceting"].append(
                        f"filterOnly({model._meta.app_label}__{model._meta.object_name}.{filter_field.field_name})"
                    )

        self.index.set_settings(index_settings)

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

import logging
from followthemoney import model

from aleph.core import es, es_index
from aleph.model import DocumentRecord
from aleph.index.mapping import TYPE_DOCUMENT, TYPE_RECORD  # noqa
from aleph.index.mapping import TYPE_COLLECTION, TYPE_ENTITY  # noqa
from aleph.index.xref import entity_query
from aleph.index.util import unpack_result
from aleph.search.parser import QueryParser, SearchQueryParser  # noqa
from aleph.search.result import QueryResult, DatabaseQueryResult  # noqa
from aleph.search.result import SearchQueryResult  # noqa
from aleph.search.query import Query, AuthzQuery

log = logging.getLogger(__name__)


class DocumentsQuery(AuthzQuery):
    DOC_TYPES = [TYPE_DOCUMENT]
    TEXT_FIELDS = ['title^3', 'summary', 'text', '_all']
    RETURN_FIELDS = ['collection_id', 'title', 'file_name', 'extension',
                     'languages', 'countries', 'source_url', 'created_at',
                     'updated_at', 'type', 'summary', 'status',
                     'error_message', 'content_hash', 'parent', '$children']
    SORT = {
        'default': ['_score', {'name_sort': 'asc'}],
        'name': [{'name_sort': 'asc'}, '_score'],
    }


class EntityDocumentsQuery(DocumentsQuery):
    """Find documents that mention an entity."""

    def __init__(self, parser, entity=None):
        super(EntityDocumentsQuery, self).__init__(parser)
        self.entity = entity

    def get_query(self):
        query = super(EntityDocumentsQuery, self).get_query()
        if self.entity is None:
            return query

        fingerprints = self.entity.get('fingerprints', [])

        for fp in fingerprints:
            query['bool']['should'].append({
                'match': {
                    'fingerprints': {
                        'query': fp,
                        'fuzziness': 1,
                        'operator': 'and',
                        'boost': 3.0
                    }
                }
            })

        names = self.entity.get('names', [])
        names.extend(fingerprints)

        for name in names:
            for field in ['title', 'summary', 'text']:
                query['bool']['should'].append({
                    'match_phrase': {
                        field: {
                            'query': name,
                            'slop': 3
                        }
                    }
                })

        # TODO: add in other entity info like phone numbers, addresses, etc.
        query['bool']['minimum_should_match'] = 1
        return query


class AlertDocumentsQuery(EntityDocumentsQuery):
    """Find documents matching an alert criterion."""

    def __init__(self, parser, entity=None, since=None):
        super(AlertDocumentsQuery, self).__init__(parser, entity=entity)
        self.since = since

    def get_highlight(self):
        return {
            'fields': {
                'text': {},
                'summary': {},
            }
        }

    def get_query(self):
        query = super(EntityDocumentsQuery, self).get_query()
        if self.since is not None:
            query['bool']['filter'].append({
                "range": {
                    "created_at": {
                        "gt": self.since
                    }
                }
            })
        return query


class EntitiesQuery(AuthzQuery):
    DOC_TYPES = [TYPE_ENTITY]
    RETURN_FIELDS = ['collection_id', 'roles', 'name', 'data', 'countries',
                     'schema', 'schemata', 'properties', 'created_at',
                     'updated_at', 'creator', '$bulk']
    SORT = {
        'default': ['_score', {'$documents': 'desc'}, {'name_sort': 'asc'}],
        'name': [{'name_sort': 'asc'}, {'$documents': 'desc'}, '_score'],
        'documents': [{'$documents': 'desc'}, {'name_sort': 'asc'}, '_score']
    }


class SimilarEntitiesQuery(EntitiesQuery):
    """Given an entity, find the most similar other entities."""

    def __init__(self, parser, entity=None):
        super(SimilarEntitiesQuery, self).__init__(parser)
        self.entity = entity

    def get_query(self):
        query = super(SimilarEntitiesQuery, self).get_query()
        return entity_query(self.entity, query=query)


class SuggestEntitiesQuery(EntitiesQuery):
    """Given a text prefix, find the most similar other entities."""
    RETURN_FIELDS = ['name', 'schema', 'fingerprints', '$documents']
    SORT = {
        'default': ['_score', {'$documents': 'desc'}, {'name_sort': 'asc'}]
    }

    def __init__(self, parser):
        super(SuggestEntitiesQuery, self).__init__(parser)

    def get_query(self):
        query = super(SuggestEntitiesQuery, self).get_query()
        query['bool']['must'] = [{
            'match_phrase_prefix': {
                'names': self.parser.prefix
            }
        }]

        # filter types which cannot be resolved via fuzzy matching.
        query['bool']['must_not'].append({
            "terms": {
                "schema": [s.name for s in model if not s.fuzzy]
            }
        })
        return query


class CombinedQuery(AuthzQuery):
    DOC_TYPES = [TYPE_ENTITY, TYPE_DOCUMENT]
    RETURN_FIELDS = set(DocumentsQuery.RETURN_FIELDS)
    RETURN_FIELDS = list(set(EntitiesQuery.RETURN_FIELDS).union(RETURN_FIELDS))

    SORT = {
        'default': ['_score', {'$documents': 'desc'}, {'name_sort': 'asc'}],
        'name': [{'name_sort': 'asc'}, {'$documents': 'desc'}, '_score']
    }


class CollectionsQuery(AuthzQuery):
    DOC_TYPES = [TYPE_COLLECTION]
    RETURN_FIELDS = True
    TEXT_FIELDS = ['label^3', '_all']
    SORT = {
        'default': [{'$total': 'desc'}, {'name_sort': 'asc'}],
        'score': ['_score', {'name_sort': 'asc'}],
        'name': [{'name_sort': 'asc'}],
    }


class MatchQueryResult(DatabaseQueryResult):
    """Matches only include entity IDs, this will expand them into entities."""

    def __init__(self, request, query, parser=None, schema=None):
        super(MatchQueryResult, self).__init__(request, query,
                                               parser=parser,
                                               schema=schema)
        ids = set()
        for match in self.results:
            ids.add(match.match_id)
            ids.add(match.entity_id)
        ids = {'ids': list(ids)}

        result = es.mget(index=es_index, doc_type=TYPE_ENTITY, body=ids)
        for doc in result.get('docs', []):
            entity = unpack_result(doc)
            if entity is None:
                continue
            for match in self.results:
                if match.match_id == entity['id']:
                    match.match = entity
                if match.entity_id == entity['id']:
                    match.entity = entity

        # Do not return results if the entity has been removed in the mean
        # time. Not sure this is the ideal way of doing this, as it'll mess
        # with pagination counts etc.
        for match in list(self.results):
            if not hasattr(match, 'match') or not hasattr(match, 'entity'):
                self.results.remove(match)


class RecordsQueryResult(SearchQueryResult):

    def __init__(self, request, parser, result, schema=None):
        super(RecordsQueryResult, self).__init__(request, parser, result,
                                                 schema=schema)
        ids = [res.get('id') for res in self.results]
        for record in DocumentRecord.find_records(ids):
            for result in self.results:
                if result['id'] == record.id:
                    result['data'] = record.data
                    result['text'] = record.text


class RecordsQuery(Query):
    DOC_TYPES = [TYPE_RECORD]
    RESULT_CLASS = RecordsQueryResult
    RETURN_FIELDS = ['document_id', 'sheet', 'index']
    TEXT_FIELDS = ['text']
    SORT = {
        'default': [{'index': 'asc'}],
        'score': ['_score', {'index': 'asc'}],
    }

    def __init__(self, parser, document=None):
        super(RecordsQuery, self).__init__(parser)
        self.document = document
        self.rows = parser.getintlist('row')

    def get_sort(self):
        if len(self.rows) or self.parser.text:
            return self.SORT.get('score')
        return super(RecordsQuery, self).get_sort()

    def get_highlight(self):
        return {
            'fields': {
                'text': {
                    'number_of_fragments': 1
                }
            }
        }

    def get_query(self):
        query = super(RecordsQuery, self).get_query()
        query['bool']['filter'].append({
            'term': {
                'document_id': self.document.id
            }
        })
        if len(self.rows):
            query['bool']['should'].append({
                "constant_score": {
                    "filter": {'terms': {'index': self.rows}},
                    "boost": 1000
                }
            })
        return query

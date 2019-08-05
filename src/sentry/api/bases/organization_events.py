from __future__ import absolute_import

from copy import deepcopy
from rest_framework.exceptions import PermissionDenied

from sentry import eventstore, features
from sentry.api.bases import OrganizationEndpoint, OrganizationEventsError
from sentry.api.event_search import get_snuba_query_args, InvalidSearchQuery
from sentry.models.project import Project
from sentry.utils import snuba

# We support 4 "special fields" on the v2 events API which perform some
# additional calculations over aggregated event data
SPECIAL_FIELDS = {
    'issue_title': {
        'aggregations': [['anyHeavy', 'title', 'issue_title']],
    },
    'last_seen': {
        'aggregations': [['max', 'timestamp', 'last_seen']],
    },
    'event_count': {
        'aggregations': [['uniq', 'id', 'event_count']],
    },
    'user_count': {
        'aggregations': [['uniq', 'user', 'user_count']],
    },
    'latest_event': {
        'fields': [
            ['argMax', ['id', 'timestamp'], 'latest_event'],
        ],
    },
}


class OrganizationEventsEndpointBase(OrganizationEndpoint):
    def get_snuba_query_args(self, request, organization, params):
        query = request.GET.get('query')

        group_ids = request.GET.getlist('group')
        if group_ids:
            # TODO(mark) This parameter should be removed in the long term.
            # Instead of using this parameter clients should use `issue.id`
            # in their query string.
            try:
                group_ids = set(map(int, filter(None, group_ids)))
            except ValueError:
                raise OrganizationEventsError('Invalid group parameter. Values must be numbers')

            projects = Project.objects.filter(
                organization=organization,
                group__id__in=group_ids,
            ).distinct()
            if any(p for p in projects if not request.access.has_project_access(p)):
                raise PermissionDenied
            params['issue.id'] = list(group_ids)
            params['project_id'] = list(set([p.id for p in projects] + params['project_id']))

        try:
            snuba_args = get_snuba_query_args(query=query, params=params)
        except InvalidSearchQuery as exc:
            raise OrganizationEventsError(exc.message)

        fields = request.GET.getlist('field')[:]
        aggregations = []
        groupby = request.GET.getlist('groupby')
        special_fields = set()

        if fields:
            # If project.name is requested, get the project.id from Snuba so we
            # can use this to look up the name in Sentry
            if 'project.name' in fields:
                fields.remove('project.name')
                if 'project.id' not in fields:
                    fields.append('project.id')

            for field in fields[:]:
                if field in SPECIAL_FIELDS:
                    special_fields.add(field)
                    special_field = deepcopy(SPECIAL_FIELDS[field])
                    fields.remove(field)
                    fields.extend(special_field.get('fields', []))
                    aggregations.extend(special_field.get('aggregations', []))
                    groupby.extend(special_field.get('groupby', []))

            snuba_args['selected_columns'] = fields

        self._filter_unspecified_special_fields_in_conditions(snuba_args, special_fields)
        if aggregations:
            snuba_args['aggregations'] = aggregations

        if groupby:
            snuba_args['groupby'] = groupby

        sort = request.GET.getlist('sort')
        if sort and snuba.valid_orderby(sort, SPECIAL_FIELDS):
            snuba_args['orderby'] = sort

        # Deprecated. `sort` should be used as it is supported by
        # more endpoints.
        orderby = request.GET.getlist('orderby')
        if orderby and snuba.valid_orderby(orderby, SPECIAL_FIELDS) and 'orderby' not in snuba_args:
            snuba_args['orderby'] = orderby

        # TODO(lb): remove once boolean search is fully functional
        has_boolean_op_flag = features.has(
            'organizations:boolean-search',
            organization,
            actor=request.user
        )
        if snuba_args.pop('has_boolean_terms', False) and not has_boolean_op_flag:
            raise OrganizationEventsError(
                'Boolean search operator OR and AND not allowed in this search.')
        return snuba_args

    def get_snuba_query_args_legacy(self, request, organization):
        params = self.get_filter_params(request, organization)

        group_ids = request.GET.getlist('group')
        if group_ids:
            # TODO(mark) This parameter should be removed in the long term.
            # Instead of using this parameter clients should use `issue.id`
            # in their query string.
            try:
                group_ids = set(map(int, filter(None, group_ids)))
            except ValueError:
                raise OrganizationEventsError('Invalid group parameter. Values must be numbers')

            projects = Project.objects.filter(
                organization=organization,
                group__id__in=group_ids,
            ).distinct()
            if any(p for p in projects if not request.access.has_project_access(p)):
                raise PermissionDenied
            params['issue.id'] = list(group_ids)
            params['project_id'] = list(set([p.id for p in projects] + params['project_id']))

        query = request.GET.get('query')
        try:
            snuba_args = get_snuba_query_args(query=query, params=params)
        except InvalidSearchQuery as exc:
            raise OrganizationEventsError(exc.message)

        # Filter out special aggregates.
        self._filter_unspecified_special_fields_in_conditions(snuba_args, set())

        # TODO(lb): remove once boolean search is fully functional
        has_boolean_op_flag = features.has(
            'organizations:boolean-search',
            organization,
            actor=request.user
        )
        if snuba_args.pop('has_boolean_terms', False) and not has_boolean_op_flag:
            raise OrganizationEventsError(
                'Boolean search operator OR and AND not allowed in this search.')
        return snuba_args

    def next_event_id(self, request, organization, snuba_args, event):
        """
        Returns the next event ID if there is a subsequent event matching the
        conditions provided. Ignores the project_id.
        """
        next_event = eventstore.get_next_event_id(
            event,
            conditions=snuba_args['conditions'],
            filter_keys=snuba_args['filter_keys']
        )

        if next_event:
            return next_event[1]

    def prev_event_id(self, request, organization, snuba_args, event):
        """
        Returns the previous event ID if there is a previous event matching the
        conditions provided. Ignores the project_id.
        """
        prev_event = eventstore.get_prev_event_id(
            event,
            conditions=snuba_args['conditions'],
            filter_keys=snuba_args['filter_keys']
        )

        if prev_event:
            return prev_event[1]

    def _filter_unspecified_special_fields_in_conditions(self, snuba_args, special_fields):
        conditions = []
        for condition in snuba_args['conditions']:
            field = condition[0]
            if (
                not isinstance(field, (list, tuple))
                and field in SPECIAL_FIELDS
                and field not in special_fields
            ):
                # skip over special field.
                continue
            conditions.append(condition)
        snuba_args['conditions'] = conditions

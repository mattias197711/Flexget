from __future__ import unicode_literals, division, absolute_import
from builtins import *
from collections import defaultdict
from datetime import datetime
from sqlalchemy import Column, String, Unicode, DateTime, Boolean
import logging

from flexget import db_schema, plugin
from flexget.entry import Entry
from flexget.utils.database import quality_property
from flexget.db_schema import Session
from flexget.event import event
from flexget.utils import qualities

log = logging.getLogger('upgrade')

Base = db_schema.versioned_base('upgrade', 0)

entry_actions = {
    'accept': Entry.accept,
    'reject': Entry.reject,
    'fail': Entry.fail
}


class EntryUpgrade(Base):
    __tablename__ = 'upgrade'

    id = Column(Unicode, primary_key=True, index=True)
    title = Column(Unicode)
    _quality = Column('quality', String)
    quality = quality_property('_quality')
    proper = Column(Boolean)
    added = Column(DateTime, index=True)

    def __init__(self):
        self.added = datetime.now()

    def __str__(self):
        return '<Upgrade(id=%s,added=%s,quality=%s)>' % \
               (self.id, self.added, self.quality)


def group_entries(entries, identified_by):
    grouped_entries = defaultdict(list)

    # Group by Identifier
    for entry in entries:
        identifier = entry.get('id') if identified_by == 'auto' else entry.render(identified_by)
        if not identifier:
            log.debug('No identifier found for', entry['title'])
            continue
        grouped_entries[identifier.lower()].append(entry)

    return grouped_entries


class LazyUpgrade(object):

    schema = {
        'oneOf': [
            {'type': 'boolean'},
            {
                'type': 'object',
                'properties': {
                    'identified_by': {'type': 'string'},
                    'tracking': {'type': 'boolean'},
                    'target': {'type': 'string', 'format': 'quality_requirements'},
                    'on_lower': {'type': 'string', 'enum': ['accept', 'reject', 'fail', 'skip']}
                },
                'additionalProperties': False
            }
        ]
    }

    def prepare_config(self, config):
        if not config or isinstance(config, bool):
            config = {}
        config.setdefault('identified_by', 'auto')
        config.setdefault('tracking', True)
        config.setdefault('on_lower', 'skip')
        config.setdefault('target', None)
        return config

    def on_task_filter(self, task, config):
        if not config:
            return

        config = self.prepare_config(config)

        grouped_entries = group_entries(task.entries, config['identified_by'])

        with Session() as session:
            # Prefetch Data
            existing_ids = session.query(EntryUpgrade).filter(EntryUpgrade.id.in_(grouped_entries.keys())).all()
            existing_ids = {e.id: e for e in existing_ids}

            for identifier, entries in grouped_entries.items():
                if not entries:
                    continue

                existing = existing_ids.get(identifier)
                if not existing:
                    # No existing, skip
                    continue

                target_downloaded = False

                if config['target']:
                    target_quality = qualities.Quality(config['target'])
                    target_requirement = qualities.Requirements(config['target'])
                    # Filter out entries within target
                    entries = list(filter(lambda e: target_requirement.allows(e['quality']), entries))

                    # Have we already downloaded target
                    if existing.quality > target_quality or target_requirement.allows(existing.quality):
                        continue

                # We may have no entries within target
                if len(entries) == 0:
                    continue

                # Sort entities in order of quality
                entries.sort(key=lambda e: e['quality'], reverse=True)

                # First entry will be the best quality
                if entries[0]['quality'] > existing.quality:
                    # Accept if we have a better quality
                    entries[0].accept('upgraded quality')

                # Action lower qualities
                if config['on_lower'] != 'skip':
                    for entry in entries:
                        action = entry_actions[config['on_lower']]
                        if entry['quality'] > existing.quality and target_downloaded:
                            msg = 'on_lower %s because already at target quality' % config['on_lower']
                            action(entry, msg)
                        if entry['quality'] < existing.quality:
                            msg = 'on_lower %s because lower then existing quality' % config['on_lower']
                            action(entry, msg)
                        if entries[0].accepted and entries[0]['quality'] > entry['quality']:
                            msg = 'on_lower %s because lower quality compared to entries' % config['on_lower']
                            action(entry, msg)

    def on_task_learn(self, task, config):
        config = self.prepare_config(config)
        if not config['tracking']:
            return

        grouped_entries = group_entries(task.accepted, config['identified_by'])

        with Session() as session:
            # Prefetch Data
            existing_ids = session.query(EntryUpgrade).filter(EntryUpgrade.id.in_(grouped_entries.keys())).all()
            existing_ids = {e.id: e for e in existing_ids}

            for identifier, entries in grouped_entries.items():
                if not entries:
                    continue

                # Sort entities in order of quality
                entries.sort(key=lambda e: e['quality'], reverse=True)
                # First entry will be the best quality
                best_entry = entries[0]

                existing = existing_ids.get(identifier)

                if not existing:
                    existing = EntryUpgrade()
                    existing.id = identifier
                    session.add(existing)
                else:
                    if existing.quality > best_entry['quality']:
                        continue

                existing.quality = best_entry['quality']
                existing.title = best_entry['title']
                existing.added = datetime.now()

                log.debug('Tracking upgrade on identifier `%s` current quality `%s`', identifier, best_entry['quality'])


@event('plugin.register')
def register_plugin():
    plugin.register(LazyUpgrade, 'upgrade', builtin=True, api_ver=2)

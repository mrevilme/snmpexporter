#!/usr/bin/env python2
import collections
import logging
import re
import multiprocessing

from snmpexporter import snmp


# How many sub-pollers to spawn to enumerate VLAN OIDs
VLAN_MAP_POOL = 2


def _poll(data):
  """Helper function that is run in a multiprocessing pool.

  This is to make VLAN context polls much faster.
  Some contexts doesn't exist and will just time out, which takes
  a loong time. So we run them in parallel.
  """
  target, vlan, oids = data
  errors = 0
  timeouts = 0
  results = {}
  for oid in oids:
    logging.debug('Collecting %s on %s @ %s', oid, target.host, vlan)
    if not oid.startswith('.1'):
      logging.warning(
          'OID %s does not start with .1, please verify configuration', oid)
      continue
    try:
      results.update(
          {(k, vlan): v for k, v in target.walk(oid, vlan).items()})
    except snmp.TimeoutError as e:
      timeouts += 1
      if vlan:
        logging.debug(
            'Timeout, is switch configured for VLAN SNMP context? %s', e)
      else:
        logging.debug('Timeout, slow switch? %s', e)
    except snmp.Error as e:
      errors += 1
      logging.warning('SNMP error for OID %s@%s: %s', oid, vlan, str(e))
  return results, errors, timeouts


class Poller(object):

  def __init__(self, collections, overrides, snmpimpl):
    super(Poller, self).__init__()
    self.model_oid_cache = {}
    self.model_oid_cache_incarnation = 0
    self.pool = multiprocessing.Pool(processes=VLAN_MAP_POOL)
    self.snmpimpl = snmpimpl
    self.collections = collections
    self.overrides = overrides

  def gather_oids(self, target, model):
    oids = set()
    vlan_aware_oids = set()
    for collection_name, collection in self.collections.items():
      for regexp in collection['models']:
        layers = collection.get('layers', None)
        if layers and target.layer not in layers:
          continue
        if 'oids' in collection and re.match(regexp, model):
          logging.debug(
              'Model %s matches collection %s', model, collection_name)
          # VLAN aware collections are run against every VLAN.
          # We don't want to run all the other OIDs (there can be a *lot* of
          # VLANs).
          vlan_aware = collection.get('vlan_aware', False)
          if vlan_aware:
            vlan_aware_oids.update(set(collection['oids']))
          else:
            oids.update(set(collection['oids']))
    return (list(oids), list(vlan_aware_oids))

  def process_overrides(self, results):
    overrides = config.get('poller', 'override')
    if not overrides:
      return results
    overridden_oids = set(overrides.keys())

    overriden_results = results
    for oid, result in results.items():
      root = '.'.join(oid.split('.')[:-1])
      if root in overridden_oids:
        overriden_results[oid] = snmp.ResultTuple(
            result.value, overrides[root])
    return overriden_results

  def poll(self, target):
    results, errors, timeouts = self._walk(target)
    results = results if results else {}
    logging.debug('Done SNMP poll (%d objects) for "%s"',
        len(list(results.keys())), target.host)
    return results, actions.Statistics(timeouts, errors)

  def _walk(self, target):
    try:
      model = self.snmpimpl.model(target)
    except snmp.TimeoutError as e:
      logging.exception('Could not determine model of %s:', target.host)
      return None, 0, 1
    except snmp.Error as e:
      logging.exception('Could not determine model of %s:', target.host)
      return None, 1, 0
    if not model:
      logging.error('Could not determine model of %s')
      return None, 1, 0

    logging.debug('Object %s is model %s', target.host, model)
    global_oids, vlan_oids = self.gather_oids(target, model)

    timeouts = 0
    errors = 0

    # 'None' is global (no VLAN aware)
    vlans = set([None])
    try:
      if vlan_oids:
        vlans.update(target.vlans())
    except snmp.Error as e:
      errors += 1
      logging.warning('Could not list VLANs: %s', str(e))

    to_poll = []
    for vlan in list(vlans):
      oids = vlan_oids if vlan else global_oids
      to_poll.append((target, vlan, oids))

    results = {}
    for part_results, part_errors, part_timeouts in self.pool.imap(
        _poll, to_poll):
      results.update(self.process_overrides(part_results))
      errors += part_errors
      timeouts += part_timeouts
    return results, errors, timeouts

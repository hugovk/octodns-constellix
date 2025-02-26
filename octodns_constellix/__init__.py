#
#
#

from collections import defaultdict
from requests import Session
from base64 import b64encode, standard_b64encode
from pycountry_convert import country_alpha2_to_continent_code
import hashlib
import hmac
import logging
import time

from octodns.record import Record
from octodns.provider import ProviderException
from octodns.provider.base import BaseProvider

__VERSION__ = '0.0.1'


class ConstellixClientException(ProviderException):
    pass


class ConstellixClientBadRequest(ConstellixClientException):

    def __init__(self, resp):
        errors = '\n  - '.join(resp.json()['errors'])
        super(ConstellixClientBadRequest, self).__init__(f'\n  - {errors}')


class ConstellixClientUnauthorized(ConstellixClientException):

    def __init__(self):
        super(ConstellixClientUnauthorized, self).__init__('Unauthorized')


class ConstellixClientNotFound(ConstellixClientException):

    def __init__(self):
        super(ConstellixClientNotFound, self).__init__('Not Found')


class ConstellixClient(object):
    BASE = 'https://api.dns.constellix.com/v1'

    def __init__(self, api_key, secret_key, ratelimit_delay=0.0):
        self.api_key = api_key
        self.secret_key = secret_key
        self.ratelimit_delay = ratelimit_delay
        self._sess = Session()
        self._sess.headers.update({'x-cnsdns-apiKey': self.api_key})
        self._domains = None
        self._pools = {'A': None, 'AAAA': None, 'CNAME': None}
        self._geofilters = None

    def _current_time(self):
        return str(int(time.time() * 1000))

    def _hmac_hash(self, now):
        return hmac.new(self.secret_key.encode('utf-8'), now.encode('utf-8'),
                        digestmod=hashlib.sha1).digest()

    def _request(self, method, path, params=None, data=None):
        now = self._current_time()
        hmac_hash = self._hmac_hash(now)

        headers = {
            'x-cnsdns-hmac': b64encode(hmac_hash),
            'x-cnsdns-requestDate': now
        }

        url = f'{self.BASE}{path}'
        resp = self._sess.request(method, url, headers=headers,
                                  params=params, json=data)
        if resp.status_code == 400:
            raise ConstellixClientBadRequest(resp)
        if resp.status_code == 401:
            raise ConstellixClientUnauthorized()
        if resp.status_code == 404:
            raise ConstellixClientNotFound()
        resp.raise_for_status()
        time.sleep(self.ratelimit_delay)
        return resp

    @property
    def domains(self):
        if self._domains is None:
            zones = []

            resp = self._request('GET', '/domains').json()
            zones += resp

            self._domains = {f'{z["name"]}.': z['id'] for z in zones}

        return self._domains

    def domain(self, name):
        zone_id = self.domains.get(name, False)
        if not zone_id:
            raise ConstellixClientNotFound()
        path = f'/domains/{zone_id}'
        return self._request('GET', path).json()

    def domain_create(self, name):
        resp = self._request('POST', '/domains', data={'names': [name]})
        # Add newly created zone to domain cache
        self._domains[f'{name}.'] = resp.json()[0]['id']

    def domain_enable_geoip(self, domain_name):
        domain = self.domain(domain_name)
        if domain['hasGeoIP'] is False:
            domain_id = self.domains[domain_name]
            self._request(
                'PUT',
                f'/domains/{domain_id}',
                data={'hasGeoIP': True}
            )

    def _absolutize_value(self, value, zone_name):
        if value == '':
            value = zone_name
        elif not value.endswith('.'):
            value = f'{value}.{zone_name}'

        return value

    def records(self, zone_name):
        zone_id = self.domains.get(zone_name, False)
        if not zone_id:
            raise ConstellixClientNotFound()
        path = f'/domains/{zone_id}/records'

        resp = self._request('GET', path).json()
        for record in resp:
            # change ANAME records to ALIAS
            if record['type'] == 'ANAME':
                record['type'] = 'ALIAS'

            # change relative values to absolute
            value = record['value']
            if record['type'] in ['ALIAS', 'CNAME', 'MX', 'NS', 'SRV']:
                if isinstance(value, str):
                    record['value'] = self._absolutize_value(value,
                                                             zone_name)
                if isinstance(value, list):
                    for v in value:
                        v['value'] = self._absolutize_value(v['value'],
                                                            zone_name)

        return resp

    def record_create(self, zone_name, record_type, params):
        # change ALIAS records to ANAME
        if record_type == 'ALIAS':
            record_type = 'ANAME'

        zone_id = self.domains.get(zone_name, False)
        path = f'/domains/{zone_id}/records/{record_type}'

        self._request('POST', path, data=params)

    def record_delete(self, zone_name, record_type, record_id):
        # change ALIAS records to ANAME
        if record_type == 'ALIAS':
            record_type = 'ANAME'

        zone_id = self.domains.get(zone_name, False)
        path = f'/domains/{zone_id}/records/{record_type}/{record_id}'
        self._request('DELETE', path)

    def pools(self, pool_type):
        if self._pools[pool_type] is None:
            self._pools[pool_type] = {}
            path = f'/pools/{pool_type}'
            response = self._request('GET', path).json()
            for pool in response:
                self._pools[pool_type][pool['id']] = pool
        return self._pools[pool_type].values()

    def pool(self, pool_type, pool_name):
        pools = self.pools(pool_type)
        for pool in pools:
            if pool['name'] == pool_name and pool['type'] == pool_type:
                return pool
        return None

    def pool_by_id(self, pool_type, pool_id):
        pools = self.pools(pool_type)
        for pool in pools:
            if pool['id'] == pool_id:
                return pool
        return None

    def pool_create(self, data):
        path = f'/pools/{data.get("type")}'
        # This returns a list of items, we want the first one
        response = self._request('POST', path, data=data).json()

        # Update our cache
        self._pools[data.get('type')][response[0]['id']] = response[0]
        return response[0]

    def pool_update(self, pool_id, data):
        path = f'/pools/{data.get("type")}/{pool_id}'
        try:
            self._request('PUT', path, data=data).json()

        except ConstellixClientBadRequest as e:
            message = str(e)
            if not message or "no changes to save" not in message:
                raise e
        return data

    def pool_delete(self, pool_type, pool_id):
        path = f'/pools/{pool_type}/{pool_id}'
        self._request('DELETE', path)

        # Update our cache
        if self._pools[pool_type] is not None:
            self._pools[pool_type].pop(pool_id, None)

    def geofilters(self):
        if self._geofilters is None:
            self._geofilters = {}
            path = '/geoFilters'
            response = self._request('GET', path).json()
            for geofilter in response:
                self._geofilters[geofilter['id']] = geofilter
        return self._geofilters.values()

    def geofilter(self, geofilter_name):
        geofilters = self.geofilters()
        for geofilter in geofilters:
            if geofilter['name'] == geofilter_name:
                return geofilter
        return None

    def geofilter_by_id(self, geofilter_id):
        geofilters = self.geofilters()
        for geofilter in geofilters:
            if geofilter['id'] == geofilter_id:
                return geofilter
        return None

    def geofilter_create(self, data):
        path = '/geoFilters'
        response = self._request('POST', path, data=data).json()

        # Update our cache
        self._geofilters[response[0]['id']] = response[0]
        return response[0]

    def geofilter_update(self, geofilter_id, data):
        path = f'/geoFilters/{geofilter_id}'
        try:
            self._request('PUT', path, data=data).json()

        except ConstellixClientBadRequest as e:
            message = str(e)
            if not message or "no changes to save" not in message:
                raise e
        return data

    def geofilter_delete(self, geofilter_id):
        path = f'/geoFilters/{geofilter_id}'
        self._request('DELETE', path)

        # Update our cache
        if self._geofilters is not None:
            self._geofilters.pop(geofilter_id, None)


class SonarClientException(ProviderException):
    pass


class SonarClientBadRequest(SonarClientException):

    def __init__(self, resp):
        errors = resp.text
        super(SonarClientBadRequest, self).__init__(f'\n  - {errors}')


class SonarClientUnauthorized(SonarClientException):

    def __init__(self):
        super(SonarClientUnauthorized, self).__init__('Unauthorized')


class SonarClientNotFound(SonarClientException):

    def __init__(self):
        super(SonarClientNotFound, self).__init__('Not Found')


class SonarClient(object):
    BASE = 'https://api.sonar.constellix.com/rest/api'

    def __init__(self, log, api_key, secret_key, ratelimit_delay=0.0):
        self.log = log
        self.api_key = api_key
        self.secret_key = secret_key
        self.ratelimit_delay = ratelimit_delay
        self._sess = Session()
        self._sess.headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'octoDNS',
        }
        self._agents = None
        self._checks = {'tcp': None, 'http': None}

    def _current_time_ms(self):
        return str(int(time.time() * 1000))

    def _hmac_hash(self, now):
        digester = hmac.new(
            bytes(self.secret_key, "UTF-8"),
            bytes(now, "UTF-8"),
            hashlib.sha1)
        signature = digester.digest()
        hmac_text = str(standard_b64encode(signature), "UTF-8")
        return hmac_text

    def _request(self, method, path, params=None, data=None):
        now = self._current_time_ms()
        hmac_text = self._hmac_hash(now)

        headers = {
            'x-cns-security-token': "{}:{}:{}".format(
                self.api_key,
                hmac_text,
                now)
        }

        url = f'{self.BASE}{path}'
        resp = self._sess.request(method, url, headers=headers,
                                  params=params, json=data)
        if resp.status_code == 400:
            raise SonarClientBadRequest(resp)
        if resp.status_code == 401:
            raise SonarClientUnauthorized()
        if resp.status_code == 404:
            raise SonarClientNotFound()
        resp.raise_for_status()

        if self.ratelimit_delay >= 1.0:
            self.log.info("Waiting for Sonar Rate Limit Delay")
        elif self.ratelimit_delay > 0.0:
            self.log.debug("Waiting for Sonar Rate Limit Delay")
        time.sleep(self.ratelimit_delay)

        return resp

    @property
    def agents(self):
        if self._agents is None:
            agents = []

            data = self._request('GET', '/system/sites').json()
            agents += data

            self._agents = {f'{a["name"]}.': a for a in agents}

        return self._agents

    def agents_for_regions(self, regions):
        if regions[0] == "WORLD":
            res_agents = []
            for agent in self.agents.values():
                res_agents.append(agent['id'])
            return res_agents

        res_agents = []
        for agent in self.agents.values():
            if agent["region"] in regions:
                res_agents.append(agent['id'])
        return res_agents

    def parse_uri_id(self, url):
        r = str(url).rfind("/")
        res = str(url)[r + 1:]
        return res

    def checks(self, check_type):
        if self._checks[check_type] is None:
            self._checks[check_type] = {}
            path = f'/{check_type}'
            data = self._request('GET', path).json()
            for check in data:
                self._checks[check_type][check['id']] = check
        return self._checks[check_type].values()

    def check(self, check_type, check_name):
        checks = self.checks(check_type)
        for check in checks:
            if check['name'] == check_name:
                return check
        return None

    def check_create(self, check_type, data):
        path = f'/{check_type}'
        response = self._request('POST', path, data=data)
        # Parse check ID from Location response header
        id = self.parse_uri_id(response.headers["Location"])
        # Get check details
        path = f'/{check_type}/{id}'
        data = self._request('GET', path, data=data).json()

        # Update our cache
        self._checks[check_type]['id'] = data
        return data

    def check_delete(self, check_id):
        # first get check type
        path = f'/check/type/{check_id}'
        data = self._request('GET', path).json()
        check_type = data['type'].lower()

        path = f'/{check_type}/{check_id}'
        self._request('DELETE', path)

        # Update our cache
        self._checks[check_type].pop(check_id, None)


class ConstellixProvider(BaseProvider):
    SUPPORTS_GEO = False
    SUPPORTS_DYNAMIC = True
    SUPPORTS = set(('A', 'AAAA', 'ALIAS', 'CAA', 'CNAME', 'MX',
                    'NS', 'PTR', 'SPF', 'SRV', 'TXT'))

    def __init__(self, id, api_key, secret_key, ratelimit_delay=0.0,
                 *args, **kwargs):
        self.log = logging.getLogger(f'ConstellixProvider[{id}]')
        self.log.debug('__init__: id=%s, api_key=***, secret_key=***', id)
        super(ConstellixProvider, self).__init__(id, *args, **kwargs)
        self._client = ConstellixClient(api_key, secret_key, ratelimit_delay)
        self._sonar = SonarClient(
            self.log, api_key, secret_key, ratelimit_delay
        )
        self._zone_records = {}

    def _data_for_multiple(self, _type, records):
        record = records[0]
        if record['recordOption'] == 'pools':
            return self._data_for_pool(_type, records)
        return {
            'ttl': record['ttl'],
            'type': _type,
            'values': record['value']
        }

    def _data_for_pool(self, _type, records):
        default_values = []
        fallback_pool_name = None
        pools = {}
        rules = []

        for record in records:
            # fetch record pool data
            pool_id = record['pools'][0]
            pool = self._client.pool_by_id(_type, pool_id)

            geofilter_id = 1
            if 'geolocation' in record.keys() \
                    and record['geolocation'] is not None:
                # fetch record geofilter data
                geofilter_id = record['geolocation']['geoipFilter']
                geofilter = self._client.geofilter_by_id(geofilter_id)

            pool_name = pool['name'].split(':')[-1]

            # fetch default values from the World Default pool
            if geofilter_id == 1:
                fallback_pool_name = pool_name
                for value in pool['values']:
                    default_values.append(value['value'])

            # populate pools
            pools[pool_name] = {
                'fallback': None,
                'values': []
            }
            for value in pool['values']:
                pools[pool_name]['values'].append({
                    'value': value['value'],
                    'weight': value['weight']
                })

            # populate rules
            if geofilter_id == 1:
                rules.append({'pool': pool_name})
            else:
                geos = []

                if 'geoipContinents' in geofilter.keys():
                    for continent_code in geofilter['geoipContinents']:
                        geos.append(continent_code)

                if 'geoipCountries' in geofilter.keys():
                    for country_code in geofilter['geoipCountries']:
                        continent_code = \
                            country_alpha2_to_continent_code(country_code)
                        geos.append(f'{continent_code}-{country_code}')

                if 'regions' in geofilter.keys():
                    for region in geofilter['regions']:
                        geos.append(f'{region["continentCode"]}-'
                                    f'{region["countryCode"]}-'
                                    f'{region["regionCode"]}')

                rules.append({
                    'pool': pool_name,
                    'geos': sorted(geos)
                })

        # set fallback pool
        for pool_name in pools:
            if pool_name != fallback_pool_name:
                pools[pool_name]['fallback'] = fallback_pool_name

        res = {
            'ttl': record['ttl'],
            'type': _type,
            'dynamic': {
                'pools': dict(
                    sorted(pools.items(), key=lambda t: t[0])),
                'rules': sorted(rules, key=lambda t: t['pool'])
            },
            'values': default_values
        }
        return res

    _data_for_A = _data_for_multiple
    _data_for_AAAA = _data_for_multiple

    def _data_for_CAA(self, _type, records):
        values = []
        record = records[0]
        for value in record['value']:
            values.append({
                'flags': value['flag'],
                'tag': value['tag'],
                'value': value['data']
            })
        return {
            'ttl': records[0]['ttl'],
            'type': _type,
            'values': values
        }

    def _data_for_NS(self, _type, records):
        record = records[0]
        return {
            'ttl': record['ttl'],
            'type': _type,
            'values': [value['value'] for value in record['value']]
        }

    def _data_for_ALIAS(self, _type, records):
        record = records[0]
        return {
            'ttl': record['ttl'],
            'type': _type,
            'value': record['value'][0]['value']
        }

    _data_for_PTR = _data_for_ALIAS

    def _data_for_TXT(self, _type, records):
        values = [value['value'].replace(';', '\\;')
                  for value in records[0]['value']]
        return {
            'ttl': records[0]['ttl'],
            'type': _type,
            'values': values
        }

    _data_for_SPF = _data_for_TXT

    def _data_for_MX(self, _type, records):
        values = []
        record = records[0]
        for value in record['value']:
            values.append({
                'preference': value['level'],
                'exchange': value['value']
            })
        return {
            'ttl': records[0]['ttl'],
            'type': _type,
            'values': values
        }

    def _data_for_single(self, _type, records):
        record = records[0]
        return {
            'ttl': record['ttl'],
            'type': _type,
            'value': record['value']
        }

    _data_for_CNAME = _data_for_single

    def _data_for_SRV(self, _type, records):
        values = []
        record = records[0]
        for value in record['value']:
            values.append({
                'port': value['port'],
                'priority': value['priority'],
                'target': value['value'],
                'weight': value['weight']
            })
        return {
            'type': _type,
            'ttl': records[0]['ttl'],
            'values': values
        }

    def zone_records(self, zone):
        if zone.name not in self._zone_records:
            try:
                self._zone_records[zone.name] = \
                    self._client.records(zone.name)
            except ConstellixClientNotFound:
                return []

        return self._zone_records[zone.name]

    def populate(self, zone, target=False, lenient=False):
        self.log.debug('populate: name=%s, target=%s, lenient=%s', zone.name,
                       target, lenient)

        values = defaultdict(lambda: defaultdict(list))
        for record in self.zone_records(zone):
            _type = record['type']
            if _type not in self.SUPPORTS:
                self.log.warning('populate: skipping unsupported %s record',
                                 _type)
                continue
            values[record['name']][record['type']].append(record)

        before = len(zone.records)
        for name, types in values.items():
            for _type, records in types.items():
                data_for = getattr(self, f'_data_for_{_type}')
                record = Record.new(zone, name, data_for(_type, records),
                                    source=self, lenient=lenient)
                zone.add_record(record, lenient=lenient)

        exists = zone.name in self._zone_records
        self.log.info('populate:   found %s records, exists=%s',
                      len(zone.records) - before, exists)
        return exists

    def _healthcheck_config(self, record):
        sonar_healthcheck = record._octodns.get('constellix', {}) \
            .get('healthcheck', None)

        if sonar_healthcheck is None:
            return None

        healthcheck = {}
        healthcheck["sonar_port"] = sonar_healthcheck.get('sonar_port', 80)
        healthcheck["sonar_type"] = sonar_healthcheck.get('sonar_type', "TCP")
        healthcheck["sonar_regions"] = sonar_healthcheck.get(
            'sonar_regions',
            ["WORLD"]
        )
        healthcheck["sonar_interval"] = sonar_healthcheck.get(
            'sonar_interval',
            "ONEMINUTE"
        )

        return healthcheck

    def _params_for_multiple(self, record):
        yield {
            'name': record.name,
            'ttl': record.ttl,
            'roundRobin': [{
                'value': value
            } for value in record.values]
        }

    _params_for_A = _params_for_multiple
    _params_for_AAAA = _params_for_multiple

    # An A record with this name must exist in this domain for
    # this NS record to be valid. Need to handle checking if
    # there is an A record before creating NS
    _params_for_NS = _params_for_multiple

    def _params_for_single(self, record):
        yield {
            'name': record.name,
            'ttl': record.ttl,
            'host': record.value,
        }

    _params_for_CNAME = _params_for_single

    def _params_for_ALIAS(self, record):
        yield {
            'name': record.name,
            'ttl': record.ttl,
            'roundRobin': [{
                'value': record.value,
                'disableFlag': False
            }]
        }

    _params_for_PTR = _params_for_ALIAS

    def _params_for_MX(self, record):
        values = []
        for value in record.values:
            values.append({
                'value': value.exchange,
                'level': value.preference
            })
        yield {
            'value': value.exchange,
            'name': record.name,
            'ttl': record.ttl,
            'roundRobin': values
        }

    def _params_for_SRV(self, record):
        values = []
        for value in record.values:
            values.append({
                'value': value.target,
                'priority': value.priority,
                'weight': value.weight,
                'port': value.port
            })
        for value in record.values:
            yield {
                'name': record.name,
                'ttl': record.ttl,
                'roundRobin': values
            }

    def _params_for_TXT(self, record):
        # Constellix does not want values escaped
        values = []
        for value in record.chunked_values:
            values.append({
                'value': value.replace('\\;', ';')
            })
        yield {
            'name': record.name,
            'ttl': record.ttl,
            'roundRobin': values
        }

    _params_for_SPF = _params_for_TXT

    def _params_for_CAA(self, record):
        values = []
        for value in record.values:
            values.append({
                'tag': value.tag,
                'data': value.value,
                'flag': value.flags,
            })
        yield {
            'name': record.name,
            'ttl': record.ttl,
            'roundRobin': values
        }

    def _handle_pools(self, record):
        healthcheck = self._healthcheck_config(record)

        # If we don't have dynamic, then there's no pools
        if not getattr(record, 'dynamic', False):
            return []

        res_pools = []

        for i, rule in enumerate(record.dynamic.rules):
            pool_name = rule.data.get('pool')
            pool = record.dynamic.pools.get(pool_name)
            values = [
                {
                    'value': value['value'],
                    'weight': value['weight'],
                } for value in pool.data.get('values', [])
            ]

            # Make a pool name based on zone, record, type and name
            generated_pool_name = \
                f'{record.zone.name}:{record.name}:{record._type}:{pool_name}'

            # Create Sonar checks if needed
            if healthcheck is not None:
                check_sites = self._sonar.\
                    agents_for_regions(healthcheck["sonar_regions"])
                for value in values:
                    check_obj = self._create_update_check(
                        pool_type = record._type,
                        check_name = '{}-{}'.format(
                            generated_pool_name,
                            value['value']
                        ),
                        check_type = healthcheck["sonar_type"].lower(),
                        value = value['value'],
                        port = healthcheck["sonar_port"],
                        interval = healthcheck["sonar_interval"],
                        sites = check_sites
                    )
                    value['checkId'] = check_obj['id']
                    value['policy'] = "followsonar"

            # OK, pool is valid, let's create it or update it
            self.log.debug("Creating pool %s", generated_pool_name)
            pool_obj = self._create_update_pool(
                pool_name = generated_pool_name,
                pool_type = record._type,
                ttl = record.ttl,
                values = values
            )

            # Now will crate GeoFilter for the pool
            continents = []
            countries = []
            regions = []

            for geo in rule.data.get('geos', []):
                codes = geo.split('-')
                n = len(geo)
                if n == 2:
                    continents.append(geo)
                elif n == 5:
                    countries.append(codes[1])
                else:
                    regions.append({
                        'continentCode': codes[0],
                        'countryCode': codes[1],
                        'regionCode': codes[2]
                    })

            if len(continents) == 0 and \
                len(countries) == 0 and \
                    len(regions) == 0:
                pool_obj['geofilter'] = 1
            else:
                self.log.debug(
                    "Creating geofilter %s",
                    generated_pool_name
                )
                geofilter_obj = self._create_update_geofilter(
                    generated_pool_name,
                    continents,
                    countries,
                    regions
                )
                pool_obj['geofilter'] = geofilter_obj['id']

            res_pools.append(pool_obj)
        return res_pools

    def _create_update_check(
            self,
            pool_type,
            check_name,
            check_type,
            value,
            port,
            interval,
            sites):

        check = {
            'name': check_name,
            'host': value,
            'port': port,
            'checkSites': sites,
            'interval': interval
        }
        if pool_type == "AAAA":
            check['ipVersion'] = "IPV6"
        else:
            check['ipVersion'] = "IPV4"

        if check_type == "http":
            check['protocolType'] = "HTTPS"

        existing_check = self._sonar.check(check_type, check_name)
        if existing_check:
            self._sonar.check_delete(existing_check['id'])

        return self._sonar.check_create(check_type, check)

    def _create_update_pool(self, pool_name, pool_type, ttl, values):
        pool = {
            'name': pool_name,
            'type': pool_type,
            'numReturn': 1,
            'minAvailableFailover': 1,
            'ttl': ttl,
            'values': values
        }
        existing_pool = self._client.pool(pool_type, pool_name)
        if not existing_pool:
            return self._client.pool_create(pool)

        pool_id = existing_pool['id']
        updated_pool = self._client.pool_update(pool_id, pool)
        updated_pool['id'] = pool_id
        return updated_pool

    def _create_update_geofilter(
            self,
            geofilter_name,
            continents,
            countries,
            regions):
        geofilter = {
            'filterRulesLimit': 100,
            'name': geofilter_name,
            'geoipContinents': continents,
            'geoipCountries': countries,
            'regions': regions
        }
        if len(regions) == 0:
            geofilter.pop('regions', None)

        existing_geofilter = self._client.geofilter(geofilter_name)
        if not existing_geofilter:
            return self._client.geofilter_create(geofilter)

        geofilter_id = existing_geofilter['id']
        updated_geofilter = self._client.geofilter_update(
            geofilter_id, geofilter)
        updated_geofilter['id'] = geofilter_id
        return updated_geofilter

    def _apply_Create(self, change, domain_name):
        new = change.new
        params_for = getattr(self, f'_params_for_{new._type}')
        pools = self._handle_pools(new)

        for params in params_for(new):
            if len(pools) == 0:
                self._client.record_create(new.zone.name, new._type, params)
            elif len(pools) == 1:
                params['pools'] = [pools[0]['id']]
                params['recordOption'] = 'pools'
                params.pop('roundRobin', None)
                self.log.debug(
                    "Creating record %s %s",
                    new.zone.name,
                    new._type
                )
                self._client.record_create(
                    new.zone.name,
                    new._type,
                    params
                )
            else:
                # To use GeoIPFilter feature we need to enable it for domain
                self.log.debug("Enabling domain %s geo support", domain_name)
                self._client.domain_enable_geoip(domain_name)

                # First we need to create World Default (1) Record
                for pool in pools:
                    if pool['geofilter'] != 1:
                        continue
                    params['pools'] = [pool['id']]
                    params['recordOption'] = 'pools'
                    params['geolocation'] = {
                        'geoipUserRegion': [pool['geofilter']]
                    }
                    params.pop('roundRobin', None)
                    self.log.debug(
                        "Creating record %s %s",
                        new.zone.name,
                        new._type)
                    self._client.record_create(
                        new.zone.name,
                        new._type,
                        params
                    )

                # Now we can create the rest of records
                for pool in pools:
                    if pool['geofilter'] == 1:
                        continue
                    params['pools'] = [pool['id']]
                    params['recordOption'] = 'pools'
                    params['geolocation'] = {
                        'geoipUserRegion': [pool['geofilter']]
                    }
                    params.pop('roundRobin', None)
                    self.log.debug(
                        "Creating record %s %s",
                        new.zone.name,
                        new._type)
                    self._client.record_create(
                        new.zone.name,
                        new._type,
                        params)

    def _apply_Update(self, change, domain_name):
        self._apply_Delete(change, domain_name)
        self._apply_Create(change, domain_name)

    def _apply_Delete(self, change, domain_name):
        existing = change.existing
        zone = existing.zone

        # if it is dynamic pools record, we need to delete World Default last
        world_default_record = None

        for record in self.zone_records(zone):
            if existing.name == record['name'] and \
               existing._type == record['type']:

                # handle dynamic record
                if record['recordOption'] == 'pools':
                    if record['geolocation'] is None:
                        world_default_record = record
                    else:
                        if record['geolocation']['geoipFilter'] == 1:
                            world_default_record = record
                        else:
                            # delete record
                            self.log.debug(
                                "Deleting record %s %s",
                                zone.name,
                                record['type'])
                            self._client.record_delete(
                                zone.name,
                                record['type'],
                                record['id'])
                            # delete geofilter
                            self.log.debug(
                                "Deleting geofilter %s",
                                zone.name)
                            self._client.geofilter_delete(
                                record['geolocation']['geoipFilter'])

                            # delete pool
                            self.log.debug(
                                "Deleting pool %s %s",
                                zone.name,
                                record['type'])
                            self._client.pool_delete(
                                record['type'],
                                record['pools'][0])

                # for all the rest records
                else:
                    self._client.record_delete(
                        zone.name, record['type'], record['id'])
        # delete World Default
        if world_default_record:
            # delete record
            self.log.debug(
                "Deleting record %s %s",
                zone.name,
                world_default_record['type']
            )
            self._client.record_delete(
                zone.name,
                world_default_record['type'],
                world_default_record['id']
            )
            # delete pool
            self.log.debug(
                "Deleting pool %s %s",
                zone.name,
                world_default_record['type']
            )
            self._client.pool_delete(
                world_default_record['type'],
                world_default_record['pools'][0]
            )

    def _apply(self, plan):
        desired = plan.desired
        changes = plan.changes
        self.log.debug('_apply: zone=%s, len(changes)=%d', desired.name,
                       len(changes))

        try:
            self._client.domain(desired.name)
        except ConstellixClientNotFound:
            self.log.debug('_apply:   no matching zone, creating domain')
            self._client.domain_create(desired.name[:-1])

        for change in changes:
            class_name = change.__class__.__name__
            getattr(self, f'_apply_{class_name}')(
                change,
                desired.name)

        # Clear out the cache if any
        self._zone_records.pop(desired.name, None)

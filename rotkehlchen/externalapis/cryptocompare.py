import glob
import logging
import os
import re
from json.decoder import JSONDecodeError
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, NamedTuple, NewType, Optional

import gevent
import requests
from typing_extensions import Literal

from rotkehlchen.assets.asset import Asset
from rotkehlchen.constants import ZERO
from rotkehlchen.constants.assets import A_BTC, A_COMP, A_DAI, A_USD, A_USDT, A_WETH
from rotkehlchen.db.dbhandler import DBHandler
from rotkehlchen.errors import (
    NoPriceForGivenTimestamp,
    PriceQueryUnsupportedAsset,
    RemoteError,
    UnsupportedAsset,
)
from rotkehlchen.externalapis.interface import ExternalServiceWithApiKey
from rotkehlchen.fval import FVal
from rotkehlchen.history import PriceHistorian
from rotkehlchen.logging import RotkehlchenLogsAdapter
from rotkehlchen.typing import ExternalService, Price, Timestamp
from rotkehlchen.utils.misc import (
    convert_to_int,
    timestamp_to_date,
    ts_now,
    write_history_data_in_file,
)
from rotkehlchen.utils.serialization import rlk_jsondumps, rlk_jsonloads_dict

logger = logging.getLogger(__name__)
log = RotkehlchenLogsAdapter(logger)


T_PairCacheKey = str
PairCacheKey = NewType('PairCacheKey', T_PairCacheKey)

RATE_LIMIT_MSG = 'You are over your rate limit please upgrade your account!'
CRYPTOCOMPARE_QUERY_RETRY_TIMES = 10
CRYPTOCOMPARE_SPECIAL_CASES_MAPPING = {
    Asset('TLN'): A_WETH,
    Asset('BLY'): A_USDT,
    Asset('cDAI'): A_DAI,
    Asset('cCOMP'): A_COMP,
    Asset('cBAT'): Asset('BAT'),
    Asset('cREP'): Asset('REP'),
    Asset('cSAI'): Asset('SAI'),
    Asset('cUSDC'): Asset('USDC'),
    Asset('cUSDT'): A_USDT,
    Asset('cWBTC'): Asset('WBTC'),
    Asset('cUNI'): Asset('UNI'),
    Asset('cZRX'): Asset('ZRX'),
    Asset('ADADOWN'): A_USDT,
    Asset('ADAUP'): A_USDT,
    Asset('BNBDOWN'): A_USDT,
    Asset('BNBUP'): A_USDT,
    Asset('BTCDOWN'): A_USDT,
    Asset('BTCUP'): A_USDT,
    Asset('ETHDOWN'): A_USDT,
    Asset('ETHUP'): A_USDT,
    Asset('EOSDOWN'): A_USDT,
    Asset('EOSUP'): A_USDT,
    Asset('DOTDOWN'): A_USDT,
    Asset('DOTUP'): A_USDT,
    Asset('LTCDOWN'): A_USDT,
    Asset('LTCUP'): A_USDT,
    Asset('TRXDOWN'): A_USDT,
    Asset('TRXUP'): A_USDT,
    Asset('XRPDOWN'): A_USDT,
    Asset('XRPUP'): A_USDT,
    Asset('DEXT'): A_USDT,
    Asset('DOS'): A_USDT,
    Asset('GEEQ'): A_USDT,
    Asset('LINKDOWN'): A_USDT,
    Asset('LINKUP'): A_USDT,
    Asset('XTZDOWN'): A_USDT,
    Asset('XTZUP'): A_USDT,
    Asset('STAKE'): A_USDT,
    Asset('MCB'): A_USDT,
    Asset('TRB'): A_USDT,
    Asset('YFI'): A_USDT,
    Asset('YAM'): A_USDT,
    Asset('DEC-2'): A_USDT,
    Asset('ORN'): A_USDT,
    Asset('PERX'): A_USDT,
    Asset('PRQ'): A_USDT,
    Asset('RING'): A_USDT,
    Asset('SBREE'): A_USDT,
    Asset('YFII'): A_USDT,
    Asset('BZRX'): A_USDT,
    Asset('CREAM'): A_USDT,
    Asset('ADEL'): A_USDT,
    Asset('ANK'): A_USDT,
    Asset('CORN'): A_USDT,
    Asset('SAL'): A_USDT,
    Asset('CRT'): A_USDT,
    Asset('FSW'): A_USDT,
    Asset('JFI'): A_USDT,
    Asset('PEARL'): A_USDT,
    Asset('TAI'): A_USDT,
    Asset('YFL'): A_USDT,
    Asset('TRUMPWIN'): A_USDT,
    Asset('TRUMPLOSE'): A_USDT,
    Asset('KLV'): A_USDT,
    Asset('KRT'): Asset('KRW'),
    Asset('RVC'): A_USDT,
    Asset('SDT'): A_USDT,
    Asset('CHI'): A_USDT,
    Asset('BAKE'): Asset('BNB'),
    Asset('BURGER'): Asset('BNB'),
    Asset('CAKE'): Asset('BNB'),
    Asset('BREE'): A_USDT,
    Asset('GHST'): A_USDT,
    Asset('MEXP'): A_USDT,
    Asset('POLS'): A_USDT,
    Asset('RARI'): A_USDT,
    Asset('VALUE'): A_USDT,
    Asset('$BASED'): A_WETH,
    Asset('DPI'): A_WETH,
    Asset('JRT'): A_USDT,
    Asset('PICKLE'): A_USDT,
    Asset('FILDOWN'): A_USDT,
    Asset('FILUP'): A_USDT,
    Asset('YFIDOWN'): A_USDT,
    Asset('YFIUP'): A_USDT,
    Asset('BOT'): A_USDT,
}
CRYPTOCOMPARE_SPECIAL_CASES = CRYPTOCOMPARE_SPECIAL_CASES_MAPPING.keys()


class PriceHistoryEntry(NamedTuple):
    time: Timestamp
    low: Price
    high: Price


class PriceHistoryData(NamedTuple):
    data: List[PriceHistoryEntry]
    start_time: Timestamp
    end_time: Timestamp


class HistoHourAssetData(NamedTuple):
    timestamp: Timestamp
    usd_price: Price


# Safest starting timestamp for requesting an asset price via histohour avoiding
# 0 price. Be aware `usd_price` is from the 'close' price in USD.
CRYPTOCOMPARE_SPECIAL_HISTOHOUR_CASES: Dict[Asset, HistoHourAssetData] = {
    A_COMP: HistoHourAssetData(
        timestamp=Timestamp(1592632800),
        usd_price=Price(FVal('202.93')),
    ),
}


def _dict_history_to_entries(data: List[Dict[str, Any]]) -> List[PriceHistoryEntry]:
    """Turns a list of dict of history entries to a list of proper objects"""
    return [
        PriceHistoryEntry(
            time=Timestamp(entry['time']),
            low=Price(FVal(entry['low'])),
            high=Price(FVal(entry['high'])),
        ) for entry in data
    ]


def _dict_history_to_data(data: Dict[str, Any]) -> PriceHistoryData:
    """Turns a price history data dict entry into a proper object"""
    return PriceHistoryData(
        data=_dict_history_to_entries(data['data']),
        start_time=Timestamp(data['start_time']),
        end_time=Timestamp(data['end_time']),
    )


def _multiply_str_nums(a: str, b: str) -> str:
    """Multiples two string numbers and returns the result as a string"""
    return str(FVal(a) * FVal(b))


def pairwise(iterable: Iterable[Any]) -> Iterator:
    "s -> (s0, s1), (s2, s3), (s4, s5), ..."
    a = iter(iterable)
    return zip(a, a)


def _check_hourly_data_sanity(
        data: List[Dict[str, Any]],
        from_asset: Asset,
        to_asset: Asset,
) -> None:
    """Check that the hourly data is an array of objects having timestamps
    increasing by 1 hour.

    If not then a RemoteError is raised
    """
    index = 0
    for n1, n2 in pairwise(data):
        diff = n2['time'] - n1['time']
        if diff != 3600:
            raise RemoteError(
                'Unexpected fata format in cryptocompare query_endpoint_histohour. '
                "Problem at indices {} and {} of {}_to_{} prices. Time difference is: {}".format(
                    index, index + 1, from_asset, to_asset, diff),
            )

        index += 2


class Cryptocompare(ExternalServiceWithApiKey):
    def __init__(self, data_directory: Path, database: Optional[DBHandler]) -> None:
        super().__init__(database=database, service_name=ExternalService.CRYPTOCOMPARE)
        self.data_directory = data_directory
        self.price_history: Dict[PairCacheKey, PriceHistoryData] = {}
        self.price_history_file: Dict[PairCacheKey, Path] = {}
        self.session = requests.session()
        self.session.headers.update({'User-Agent': 'rotkehlchen'})

        # Check the data folder and remember the filenames of any cached history
        prefix = os.path.join(str(self.data_directory), 'price_history_')
        prefix = prefix.replace('\\', '\\\\')
        regex = re.compile(prefix + r'(.*)\.json')
        files_list = glob.glob(prefix + '*.json')

        for file_ in files_list:
            file_ = file_.replace('\\\\', '\\')
            match = regex.match(file_)
            assert match
            cache_key = PairCacheKey(match.group(1))
            self.price_history_file[cache_key] = Path(file_)

    def set_database(self, database: DBHandler) -> None:
        """If the cryptocompare instance was initialized without a DB this sets its DB"""
        msg = 'set_database was called on a cryptocompare instance that already has a DB'
        assert self.db is None, msg
        self.db = database

    def unset_database(self) -> None:
        """Remove the database connection from this cryptocompare instance

        This should happen when a user logs out"""
        msg = 'unset_database was called on a cryptocompare instance that has no DB'
        assert self.db is not None, msg
        self.db = None

    def _api_query(self, path: str) -> Dict[str, Any]:
        """Queries cryptocompare

        - May raise RemoteError if there is a problem reaching the cryptocompare server
        or with reading the response returned by the server
        """
        querystr = f'https://min-api.cryptocompare.com/data/{path}'
        log.debug('Querying cryptocompare', url=querystr)
        api_key = self._get_api_key()
        if api_key:
            querystr += f'&api_key={api_key}'

        tries = CRYPTOCOMPARE_QUERY_RETRY_TIMES
        while tries >= 0:
            try:
                response = self.session.get(querystr)
            except requests.exceptions.ConnectionError as e:
                raise RemoteError(f'Cryptocompare API request failed due to {str(e)}')

            try:
                json_ret = rlk_jsonloads_dict(response.text)
            except JSONDecodeError:
                raise RemoteError(f'Cryptocompare returned invalid JSON response: {response.text}')

            try:
                if json_ret.get('Message', None) == RATE_LIMIT_MSG:
                    if tries >= 1:
                        backoff_seconds = 20 / tries
                        log.debug(
                            f'Got rate limited by cryptocompare. '
                            f'Backing off for {backoff_seconds}',
                        )
                        gevent.sleep(backoff_seconds)
                        tries -= 1
                        continue
                    else:
                        log.debug(
                            f'Got rate limited by cryptocompare and did not manage to get a '
                            f'request through even after {CRYPTOCOMPARE_QUERY_RETRY_TIMES} '
                            f'incremental backoff retries',
                        )

                if json_ret.get('Response', 'Success') != 'Success':
                    error_message = f'Failed to query cryptocompare for: "{querystr}"'
                    if 'Message' in json_ret:
                        error_message += f'. Error: {json_ret["Message"]}'

                    log.error(
                        'Cryptocompare query failure',
                        url=querystr,
                        error=error_message,
                        status_code=response.status_code,
                    )
                    raise RemoteError(error_message)
                return json_ret['Data'] if 'Data' in json_ret else json_ret
            except KeyError as e:
                raise RemoteError(
                    f'Unexpected format of Cryptocompare json_response. '
                    f'Missing key entry for {str(e)}',
                )

        raise AssertionError('We should never get here')

    def _special_case_handling(
            self,
            method_name: Literal[
                'query_endpoint_histohour',
                'query_endpoint_price',
                'query_endpoint_pricehistorical',
            ],
            from_asset: Asset,
            to_asset: Asset,
            **kwargs: Any,
    ) -> Any:
        """Special case handling for queries that need combination of multiple asset queries

        This is hopefully temporary and can be taken care of by cryptocompare itself in the future.

        For some assets cryptocompare can only figure out the price via intermediaries.
        This function takes care of these special cases."""
        method = getattr(self, method_name)
        intermediate_asset = CRYPTOCOMPARE_SPECIAL_CASES_MAPPING[from_asset]
        result1 = method(
            from_asset=from_asset,
            to_asset=intermediate_asset,
            handling_special_case=True,
            **kwargs,
        )
        result2 = method(
            from_asset=intermediate_asset,
            to_asset=to_asset,
            handling_special_case=True,
            **kwargs,
        )
        result: Any
        if method_name == 'query_endpoint_histohour':
            result = {
                'Aggregated': result1['Aggregated'],
                'TimeFrom': result1['TimeFrom'],
                'TimeTo': result1['TimeTo'],
            }
            result1 = result1['Data']
            result2 = result2['Data']
            data = []
            for idx, entry in enumerate(result1):
                entry2 = result2[idx]
                data.append({
                    'time': entry['time'],
                    'high': _multiply_str_nums(entry['high'], entry2['high']),
                    'low': _multiply_str_nums(entry['low'], entry2['low']),
                    'open': _multiply_str_nums(entry['open'], entry2['open']),
                    'volumefrom': entry['volumefrom'],
                    'volumeto': entry['volumeto'],
                    'close': _multiply_str_nums(entry['close'], entry2['close']),
                    'conversionType': entry['conversionType'],
                    'conversionSymbol': entry['conversionSymbol'],
                })
            result['Data'] = data
        elif method_name == 'query_endpoint_price':
            result = {
                to_asset.identifier: _multiply_str_nums(
                    # up until 23/09/2020 cryptocompare may return {} due to bug. Handle
                    # that case by assuming 0 if that happens
                    result1.get(intermediate_asset.identifier, '0'),
                    result2.get(to_asset.identifier, '0'),
                ),
            }
        elif method_name == 'query_endpoint_pricehistorical':
            result = result1 * result2
        else:
            raise RuntimeError(f'Illegal method_name: {method_name}. Should never happen')

        return result

    def query_endpoint_histohour(
            self,
            from_asset: Asset,
            to_asset: Asset,
            limit: int,
            to_timestamp: Timestamp,
            handling_special_case: bool = False,
    ) -> Dict[str, Any]:
        """Returns the full histohour response including TimeFrom and TimeTo

        - May raise RemoteError if there is a problem reaching the cryptocompare server
        or with reading the response returned by the server
        - May raise PriceQueryUnsupportedAsset if from/to assets are not known to cryptocompare
        """
        special_asset = (
            from_asset in CRYPTOCOMPARE_SPECIAL_CASES or to_asset in CRYPTOCOMPARE_SPECIAL_CASES
        )
        if special_asset and not handling_special_case:
            return self._special_case_handling(
                method_name='query_endpoint_histohour',
                from_asset=from_asset,
                to_asset=to_asset,
                limit=limit,
                to_timestamp=to_timestamp,
            )

        try:
            cc_from_asset_symbol = from_asset.to_cryptocompare()
            cc_to_asset_symbol = to_asset.to_cryptocompare()
        except UnsupportedAsset as e:
            raise PriceQueryUnsupportedAsset(e.asset_name)

        query_path = (
            f'v2/histohour?fsym={cc_from_asset_symbol}&tsym={cc_to_asset_symbol}'
            f'&limit={limit}&toTs={to_timestamp}'
        )
        result = self._api_query(path=query_path)
        return result

    def query_endpoint_price(
            self,
            from_asset: Asset,
            to_asset: Asset,
            handling_special_case: bool = False,
    ) -> Dict[str, Any]:
        """Returns the current price of an asset compared to another asset

        - May raise RemoteError if there is a problem reaching the cryptocompare server
        or with reading the response returned by the server
        - May raise PriceQueryUnsupportedAsset if from/to assets are not known to cryptocompare
        """
        special_asset = (
            from_asset in CRYPTOCOMPARE_SPECIAL_CASES or to_asset in CRYPTOCOMPARE_SPECIAL_CASES
        )
        if special_asset and not handling_special_case:
            return self._special_case_handling(
                method_name='query_endpoint_price',
                from_asset=from_asset,
                to_asset=to_asset,
            )
        try:
            cc_from_asset_symbol = from_asset.to_cryptocompare()
            cc_to_asset_symbol = to_asset.to_cryptocompare()
        except UnsupportedAsset as e:
            raise PriceQueryUnsupportedAsset(e.asset_name)

        query_path = f'price?fsym={cc_from_asset_symbol}&tsyms={cc_to_asset_symbol}'
        result = self._api_query(path=query_path)
        return result

    def query_endpoint_pricehistorical(
            self,
            from_asset: Asset,
            to_asset: Asset,
            timestamp: Timestamp,
            handling_special_case: bool = False,
    ) -> Price:
        """Queries the historical daily price of from_asset to to_asset for timestamp

        - May raise RemoteError if there is a problem reaching the cryptocompare server
        or with reading the response returned by the server
        - May raise PriceQueryUnsupportedAsset if from/to assets are not known to cryptocompare
        """
        log.debug(
            'Querying cryptocompare for daily historical price',
            from_asset=from_asset,
            to_asset=to_asset,
            timestamp=timestamp,
        )
        special_asset = (
            from_asset in CRYPTOCOMPARE_SPECIAL_CASES or to_asset in CRYPTOCOMPARE_SPECIAL_CASES
        )
        if special_asset and not handling_special_case:
            return self._special_case_handling(
                method_name='query_endpoint_pricehistorical',
                from_asset=from_asset,
                to_asset=to_asset,
                timestamp=timestamp,
            )

        try:
            cc_from_asset_symbol = from_asset.to_cryptocompare()
            cc_to_asset_symbol = to_asset.to_cryptocompare()
        except UnsupportedAsset as e:
            raise PriceQueryUnsupportedAsset(e.asset_name)

        query_path = (
            f'pricehistorical?fsym={cc_from_asset_symbol}&tsyms={cc_to_asset_symbol}'
            f'&ts={timestamp}'
        )
        if to_asset == 'BTC':
            query_path += '&tryConversion=false'
        result = self._api_query(query_path)
        return Price(FVal(result[cc_from_asset_symbol][cc_to_asset_symbol]))

    def _got_cached_price(self, cache_key: PairCacheKey, timestamp: Timestamp) -> bool:
        """Check if we got a price history for the timestamp cached"""
        if cache_key in self.price_history_file:
            if cache_key not in self.price_history:
                try:
                    with open(self.price_history_file[cache_key], 'r') as f:
                        data = rlk_jsonloads_dict(f.read())
                        self.price_history[cache_key] = _dict_history_to_data(data)
                except (OSError, JSONDecodeError):
                    return False

            in_range = (
                self.price_history[cache_key].start_time <= timestamp and
                self.price_history[cache_key].end_time > timestamp
            )
            if in_range:
                log.debug('Found cached price', cache_key=cache_key, timestamp=timestamp)
                return True

        return False

    def get_historical_data(
            self,
            from_asset: Asset,
            to_asset: Asset,
            timestamp: Timestamp,
            historical_data_start: Timestamp,
    ) -> List[PriceHistoryEntry]:
        """
        Get historical price data from cryptocompare

        Returns a sorted list of price entries.

        - May raise RemoteError if there is a problem reaching the cryptocompare server
        or with reading the response returned by the server
        - May raise UnsupportedAsset if from/to asset is not supported by cryptocompare
        """
        log.debug(
            'Retrieving historical price data from cryptocompare',
            from_asset=from_asset,
            to_asset=to_asset,
            timestamp=timestamp,
        )

        cache_key = PairCacheKey(from_asset.identifier + '_' + to_asset.identifier)
        got_cached_value = self._got_cached_price(cache_key, timestamp)
        if got_cached_value:
            return self.price_history[cache_key].data

        now_ts = ts_now()
        cryptocompare_hourquerylimit = 2000
        calculated_history: List[Dict[str, Any]] = []

        if historical_data_start <= timestamp:
            end_date = historical_data_start
        else:
            end_date = timestamp
        while True:
            pr_end_date = end_date
            end_date = Timestamp(end_date + (cryptocompare_hourquerylimit) * 3600)

            log.debug(
                'Querying cryptocompare for hourly historical price',
                from_asset=from_asset,
                to_asset=to_asset,
                cryptocompare_hourquerylimit=cryptocompare_hourquerylimit,
                end_date=end_date,
            )

            resp = self.query_endpoint_histohour(
                from_asset=from_asset,
                to_asset=to_asset,
                limit=2000,
                to_timestamp=end_date,
            )

            if pr_end_date != resp['TimeFrom']:
                # If we get more than we needed, since we are close to the now_ts
                # then skip all the already included entries
                diff = pr_end_date - resp['TimeFrom']
                # If the start date has less than 3600 secs difference from previous
                # end date then do nothing. If it has more skip all already included entries
                if diff >= 3600:
                    if resp['Data'][diff // 3600]['time'] != pr_end_date:
                        raise RemoteError(
                            'Unexpected fata format in cryptocompare query_endpoint_histohour. '
                            'Expected to find the previous date timestamp during '
                            'cryptocompare historical data fetching',
                        )
                    # just add only the part from the previous timestamp and on
                    resp['Data'] = resp['Data'][diff // 3600:]

            # The end dates of a cryptocompare query do not match. The end date
            # can have up to 3600 secs different to the requested one since this is
            # hourly historical data but no more.
            end_dates_dont_match = (
                end_date < now_ts and
                resp['TimeTo'] != end_date
            )
            if end_dates_dont_match:
                if resp['TimeTo'] - end_date >= 3600:
                    raise RemoteError(
                        'Unexpected fata format in cryptocompare query_endpoint_histohour. '
                        'End dates do not match.',
                    )
                else:
                    # but if it's just a drift within an hour just update the end_date so that
                    # it can be picked up by the next iterations in the loop
                    end_date = resp['TimeTo']

            # If last time slot and first new are the same, skip the first new slot
            last_entry_equal_to_first = (
                len(calculated_history) != 0 and
                calculated_history[-1]['time'] == resp['Data'][0]['time']
            )
            if last_entry_equal_to_first:
                resp['Data'] = resp['Data'][1:]
            calculated_history += resp['Data']
            if end_date >= now_ts:
                break

        # Let's always check for data sanity for the hourly prices.
        _check_hourly_data_sanity(calculated_history, from_asset, to_asset)
        # and now since we actually queried the data let's also cache them
        filename = self.data_directory / ('price_history_' + cache_key + '.json')
        log.info(
            'Updating price history cache',
            filename=filename,
            from_asset=from_asset,
            to_asset=to_asset,
        )
        write_history_data_in_file(
            data=calculated_history,
            filepath=filename,
            start_ts=historical_data_start,
            end_ts=now_ts,
        )

        # Finally save the objects in memory and return them
        data_including_time = {
            'data': calculated_history,
            'start_time': historical_data_start,
            'end_time': end_date,
        }
        self.price_history_file[cache_key] = filename
        self.price_history[cache_key] = _dict_history_to_data(data_including_time)

        return self.price_history[cache_key].data

    @staticmethod
    def _check_and_get_special_histohour_price(
            from_asset: Asset,
            to_asset: Asset,
            timestamp: Timestamp,
    ) -> Price:
        """For the given timestamp, check whether the from..to asset price
        (or viceversa) is a special histohour API case. If so, return the price
        based on the assets pair, otherwise return zero.

        NB: special histohour API cases are the one where this Cryptocompare
        API returns zero prices per hour.
        """
        price = Price(ZERO)
        if (
            from_asset in CRYPTOCOMPARE_SPECIAL_HISTOHOUR_CASES and to_asset == A_USD or
            from_asset == A_USD and to_asset in CRYPTOCOMPARE_SPECIAL_HISTOHOUR_CASES
        ):
            asset_data = (
                CRYPTOCOMPARE_SPECIAL_HISTOHOUR_CASES[from_asset]
                if to_asset == A_USD
                else CRYPTOCOMPARE_SPECIAL_HISTOHOUR_CASES[to_asset]
            )
            if timestamp <= asset_data.timestamp:
                price = (
                    asset_data.usd_price
                    if to_asset == A_USD
                    else Price(FVal('1') / asset_data.usd_price)
                )
                log.warning(
                    f'Query price of: {from_asset.identifier} in {to_asset.identifier} '
                    f'at timestamp {timestamp} may return zero price. '
                    f'Setting price to {price}, from timestamp {asset_data.timestamp}.',
                )
        return price

    def query_historical_price(
            self,
            from_asset: Asset,
            to_asset: Asset,
            timestamp: Timestamp,
            historical_data_start: Timestamp,
    ) -> Price:
        """
        Query the historical price on `timestamp` for `from_asset` in `to_asset`.
        So how much `to_asset` does 1 unit of `from_asset` cost.

        May raise:
        - PriceQueryUnsupportedAsset if from/to asset is known to miss from cryptocompare
        - NoPriceForGivenTimestamp if we can't find a price for the asset in the given
        timestamp from cryptocompare
        - RemoteError if there is a problem reaching the cryptocompare server
        or with reading the response returned by the server
        """
        # NB: check if the from..to asset price (or viceversa) is a special
        # histohour API case.
        price = self._check_and_get_special_histohour_price(
            from_asset=from_asset,
            to_asset=to_asset,
            timestamp=timestamp,
        )
        if price != Price(ZERO):
            return price

        try:
            data = self.get_historical_data(
                from_asset=from_asset,
                to_asset=to_asset,
                timestamp=timestamp,
                historical_data_start=historical_data_start,
            )
        except UnsupportedAsset as e:
            raise PriceQueryUnsupportedAsset(e.asset_name)

        price = Price(ZERO)
        # all data are sorted and timestamps are always increasing by 1 hour
        # find the closest entry to the provided timestamp
        if timestamp >= data[0].time:
            index_in_bounds = True
            # convert_to_int can't raise here due to its input
            index = convert_to_int((timestamp - data[0].time) / 3600, accept_only_exact=False)
            if index > len(data) - 1:  # index out of bounds
                # Try to see if index - 1 is there and if yes take it
                if index > len(data):
                    index = index - 1
                else:  # give up. This happened: https://github.com/rotki/rotki/issues/1534
                    log.error(
                        f'Expected data index in cryptocompare historical hour price '
                        f'not found. Queried price of: {from_asset.identifier} in '
                        f'{to_asset.identifier} at {timestamp}. Data '
                        f'index: {index}. Length of returned data: {len(data)}. '
                        f'https://github.com/rotki/rotki/issues/1534. Attempting other methods...',
                    )
                    index_in_bounds = False

            if index_in_bounds:
                diff = abs(data[index].time - timestamp)
                if index + 1 <= len(data) - 1:
                    diff_p1 = abs(data[index + 1].time - timestamp)
                    if diff_p1 < diff:
                        index = index + 1

                if data[index].high is not None and data[index].low is not None:
                    price = Price((data[index].high + data[index].low) / 2)

        else:
            # no price found in the historical data from/to asset, try alternatives
            price = Price(ZERO)

        if price == 0:
            if from_asset != 'BTC' and to_asset != 'BTC':
                log.debug(
                    f"Couldn't find historical price from {from_asset} to "
                    f"{to_asset} at timestamp {timestamp}. Comparing with BTC...",
                )
                # Just get the BTC price
                asset_btc_price = PriceHistorian().query_historical_price(
                    from_asset=from_asset,
                    to_asset=A_BTC,
                    timestamp=timestamp,
                )
                btc_to_asset_price = PriceHistorian().query_historical_price(
                    from_asset=A_BTC,
                    to_asset=to_asset,
                    timestamp=timestamp,
                )
                price = Price(asset_btc_price * btc_to_asset_price)
            else:
                log.debug(
                    f"Couldn't find historical price from {from_asset} to "
                    f"{to_asset} at timestamp {timestamp} through cryptocompare."
                    f" Attempting to get daily price...",
                )
                price = self.query_endpoint_pricehistorical(from_asset, to_asset, timestamp)

        comparison_to_nonusd_fiat = (
            (to_asset.is_fiat() and to_asset != A_USD) or
            (from_asset.is_fiat() and from_asset != A_USD)
        )
        if comparison_to_nonusd_fiat:
            price = self._adjust_to_cryptocompare_price_incosistencies(
                price=price,
                from_asset=from_asset,
                to_asset=to_asset,
                timestamp=timestamp,
            )

        if price == 0:
            raise NoPriceForGivenTimestamp(
                from_asset=from_asset,
                to_asset=to_asset,
                date=timestamp_to_date(timestamp, formatstr='%d/%m/%Y, %H:%M:%S'),
            )

        log.debug(
            'Got historical price',
            from_asset=from_asset,
            to_asset=to_asset,
            timestamp=timestamp,
            price=price,
        )

        return price

    @staticmethod
    def _adjust_to_cryptocompare_price_incosistencies(
            price: Price,
            from_asset: Asset,
            to_asset: Asset,
            timestamp: Timestamp,
    ) -> Price:
        """Doublecheck against the USD rate, and if incosistencies are found
        then take the USD adjusted price.

        This is due to incosistencies in the provided historical data from
        cryptocompare. https://github.com/rotki/rotki/issues/221

        Note: Since 12/01/2019 this seems to no longer be happening, but I will
        keep the code around just in case a regression is introduced on the side
        of cryptocompare.

        May raise:
        - PriceQueryUnsupportedAsset if the from asset is known to miss from cryptocompare
        - NoPriceForGivenTimestamp if we can't find a price for the asset in the given
        timestamp from cryptocompare
        - RemoteError if there is a problem reaching the cryptocompare server
        or with reading the response returned by the server
        """
        from_asset_usd = PriceHistorian().query_historical_price(
            from_asset=from_asset,
            to_asset=A_USD,
            timestamp=timestamp,
        )
        to_asset_usd = PriceHistorian().query_historical_price(
            from_asset=to_asset,
            to_asset=A_USD,
            timestamp=timestamp,
        )

        usd_invert_conversion = Price(from_asset_usd / to_asset_usd)
        abs_diff = abs(usd_invert_conversion - price)
        relative_difference = abs_diff / max(price, usd_invert_conversion)
        if relative_difference >= FVal('0.1'):
            log.warning(
                'Cryptocompare historical price data are incosistent.'
                'Taking USD adjusted price. Check github issue #221',
                from_asset=from_asset,
                to_asset=to_asset,
                incosistent_price=price,
                usd_price=from_asset_usd,
                adjusted_price=usd_invert_conversion,
            )
            return usd_invert_conversion
        return price

    def all_coins(self) -> Dict[str, Any]:
        """
        Gets the list of all the cryptocompare coins

        May raise:
        - RemoteError if there is a problem reaching the cryptocompare server
        or with reading the response returned by the server
        """
        # Get coin list of cryptocompare
        invalidate_cache = True
        coinlist_cache_path = os.path.join(self.data_directory, 'cryptocompare_coinlist.json')
        if os.path.isfile(coinlist_cache_path):
            log.info('Found cryptocompare coinlist cache', path=coinlist_cache_path)
            with open(coinlist_cache_path, 'r') as f:
                try:
                    data = rlk_jsonloads_dict(f.read())
                    now = ts_now()
                    invalidate_cache = False

                    # If we got a cache and its' over a month old then requery cryptocompare
                    if data['time'] < now and now - data['time'] > 2629800:
                        log.info('Cryptocompare coinlist cache is now invalidated')
                        invalidate_cache = True
                        data = data['data']
                except JSONDecodeError:
                    invalidate_cache = True

        if invalidate_cache:
            data = self._api_query('all/coinlist')

            # Also save the cache
            with open(coinlist_cache_path, 'w') as f:
                now = ts_now()
                log.info('Writing coinlist cache', timestamp=now)
                write_data = {'time': now, 'data': data}
                f.write(rlk_jsondumps(write_data))
        else:
            # in any case take the data
            data = data['data']

        # As described in the docs
        # https://min-api.cryptocompare.com/documentation?key=Other&cat=allCoinsWithContentEndpoint
        # This is not the entire list of assets in the system, so I am manually adding
        # here assets I am aware of that they already have historical data for in thei
        # cryptocompare system
        data['DAO'] = object()
        data['USDT'] = object()
        data['VEN'] = object()
        data['AIR*'] = object()  # This is Aircoin
        # This is SpendCoin (https://coinmarketcap.com/currencies/spendcoin/)
        data['SPND'] = object()
        # This is eBitcoinCash (https://coinmarketcap.com/currencies/ebitcoin-cash/)
        data['EBCH'] = object()
        # This is Educare (https://coinmarketcap.com/currencies/educare/)
        data['EKT'] = object()
        # This is Knoxstertoken (https://coinmarketcap.com/currencies/knoxstertoken/)
        data['FKX'] = object()
        # This is FNKOS (https://coinmarketcap.com/currencies/fnkos/)
        data['FNKOS'] = object()
        # This is FansTime (https://coinmarketcap.com/currencies/fanstime/)
        data['FTI'] = object()
        # This is Gene Source Code Chain
        # (https://coinmarketcap.com/currencies/gene-source-code-chain/)
        data['GENE*'] = object()
        # This is GazeCoin (https://coinmarketcap.com/currencies/gazecoin/)
        data['GZE'] = object()
        # This is probaly HarmonyCoin (https://coinmarketcap.com/currencies/harmonycoin-hmc/)
        data['HMC*'] = object()
        # This is IoTChain (https://coinmarketcap.com/currencies/iot-chain/)
        data['ITC'] = object()
        # This is Luna Coin (https://coinmarketcap.com/currencies/luna-coin/)
        data['LUNA'] = object
        # This is MFTU (https://coinmarketcap.com/currencies/mainstream-for-the-underground/)
        data['MFTU'] = object()
        # This is Nexxus (https://coinmarketcap.com/currencies/nexxus/)
        data['NXX'] = object()
        # This is Owndata (https://coinmarketcap.com/currencies/owndata/)
        data['OWN'] = object()
        # This is PiplCoin (https://coinmarketcap.com/currencies/piplcoin/)
        data['PIPL'] = object()
        # This is PKG Token (https://coinmarketcap.com/currencies/pkg-token/)
        data['PKG'] = object()
        # This is Quibitica https://coinmarketcap.com/currencies/qubitica/
        data['QBIT'] = object()
        # This is DPRating https://coinmarketcap.com/currencies/dprating/
        data['RATING'] = object()
        # This is RouletteToken https://coinmarketcap.com/currencies/roulettetoken/
        data['RLT'] = object()
        # This is RocketPool https://coinmarketcap.com/currencies/rocket-pool/
        data['RPL'] = object()
        # This is SpeedMiningService (https://coinmarketcap.com/currencies/speed-mining-service/)
        data['SMS'] = object()
        # This is SmartShare (https://coinmarketcap.com/currencies/smartshare/)
        data['SSP'] = object()
        # This is ThoreCoin (https://coinmarketcap.com/currencies/thorecoin/)
        data['THR'] = object()
        # This is Transcodium (https://coinmarketcap.com/currencies/transcodium/)
        data['TNS'] = object()

        return data

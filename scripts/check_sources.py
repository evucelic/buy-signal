"""Isolated source/resource smoke check; temporary files only, never Telegram.

python -m scripts.check_sources [--browser] [--earnings]
Use on the deployment host to verify actual source access from its public IP.
"""

import argparse
import json
from pathlib import Path
import resource
from tempfile import TemporaryDirectory
import time

from buysignal.collectors import credit, entry, fed_rate, margin_debt, sectors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', action='store_true', help='Include one CME browser scrape')
    parser.add_argument('--earnings', action='store_true', help='Include industry/constituent estimates')
    args = parser.parse_args()
    result = {}
    started = time.perf_counter()
    with TemporaryDirectory(prefix='buysignal-source-check-') as directory:
        root = Path(directory)
        if not entry.refresh_entry_history(root/'prices.csv'):
            raise RuntimeError('Adjusted price download failed')
        history = entry.entry_history(root/'prices.csv')
        result['prices'] = {'rows': len(history), 'first': str(history.index[0].date()), 'last': str(history.index[-1].date())}
        for name, update, read, date_column in (
            ('credit', credit.update_credit_data, credit.credit_history, 'date'),
            ('margin', margin_debt.update_margin_debt_data, margin_debt.margin_history, 'month'),
        ):
            error = update(root/f'{name}.csv')
            if error:
                raise RuntimeError(error)
            history = read(root/f'{name}.csv')
            result[name] = {'rows': len(history), 'first': str(history[date_column].min().date()), 'last': str(history[date_column].max().date())}
        if args.earnings:
            error = sectors.update_sector_data(root/'industries.csv')
            if error:
                raise RuntimeError(error)
            result['industries'] = sectors.sector_performance(root/'industries.csv')[['industry', 'estimate_coverage', 'covered_companies']].to_dict(orient='records')
        if args.browser:
            from buysignal.collectors import chrome
            chrome.CHROMEDRIVER_LOG = root/'chrome.log'
            snapshot = fed_rate._validate_snapshot(fed_rate._fetch_meeting_probabilities(fed_rate._today()), fed_rate._today())
            result['fedwatch'] = {'rows': len(snapshot), 'reference_lower_bps': int(snapshot.reference_lower_bps.iloc[0]), 'reference_upper_bps': int(snapshot.reference_upper_bps.iloc[0])}
        result['elapsed_seconds'] = round(time.perf_counter()-started, 2)
        result['process_peak_rss_mib'] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024, 1)
        peak = Path('/sys/fs/cgroup/memory.peak')
        if peak.exists():
            result['container_memory_peak_bytes'] = peak.read_text().strip()
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()

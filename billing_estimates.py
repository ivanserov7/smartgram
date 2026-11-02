"""Choose actual allocations first, then dated tariff estimates of observed usage.

This display layer never changes the Costs ledger or its unallocated balance.
Unknown key/date prevents allocation, but not a clearly labelled tariff estimate.
"""
from collections import defaultdict
from decimal import Decimal, localcontext

from billing_api import money_text
from billing_units import count, consumption, billed_searches

PRICE_URL = 'https://developers.openai.com/api/docs/pricing'
PRICE_VERSION = 'openai-public-2026-11-01'
LABELS = {'actual': 'Факт · распределено', 'estimated': 'Оценка',
          'mixed': 'Факт + оценка', 'unknown': 'Нет данных'}
TEXT_RATES = {
    'gpt-6-luna': ('0.10', '0.01', '0.125', '0.50'),
    'gpt-4.1-mini': ('0.40', '0.10', '0', '1.60'),
    'gpt-4.1-mini-2025-04-14': ('0.40', '0.10', '0', '1.60'),
}
AUDIO_RATES = {
    'gpt-realtime-2.1': {'input_text_tokens': '4', 'output_text_tokens': '24',
                         'input_audio_tokens': '32', 'output_audio_tokens': '64', 'input_image_tokens': '5'},
    'gpt-realtime-2.1-mini': {'input_text_tokens': '0.60', 'output_text_tokens': '2.40',
                              'input_audio_tokens': '10', 'output_audio_tokens': '20', 'input_image_tokens': '0.80'},
    'gpt-audio-1.5': {'input_text_tokens': '2.50', 'output_text_tokens': '10',
                      'input_audio_tokens': '32', 'output_audio_tokens': '64'},
    'gpt-4o-mini-tts': {'input_text_tokens': '0.60', 'output_audio_tokens': '12'},
}


def estimate_components(call):
    """Return priced observed components, explicit assumptions, and unpriced gaps."""
    units = call.get('units') or {}; usage = call.get('usage') or {}
    model = call.get('model'); components = {}; assumptions = []; missing = []

    def add(category, quantity, rate, divisor=1000000):
        if quantity is None:
            missing.append('Нет потребления: ' + category); return
        if not isinstance(quantity, (int, float, Decimal)) or isinstance(quantity, bool):
            missing.append('Некорректное потребление: ' + category); return
        q = Decimal(str(quantity))
        if not q.is_finite() or q < 0:
            missing.append('Некорректное потребление: ' + category); return
        components[category] = {'category': category, 'kind': 'estimated', 'currency': 'usd',
            'units': money_text(q), 'rate': str(rate), 'rate_divisor': divisor,
            'amount': money_text(q * Decimal(rate) / Decimal(divisor))}

    tier = call.get('service_tier'); batch = call.get('batch')
    factor = Decimal(1)
    if tier is None: assumptions.append('Режим неизвестен: использован публичный Standard-тариф.')
    if batch is None: assumptions.append('Batch неизвестен: использован обычный запрос.')
    if tier in ('flex',) or batch is True: factor = Decimal('0.5')
    elif tier in ('priority', 'fast') and model == 'gpt-6-luna': factor = Decimal(2)
    elif tier not in (None, 'default', 'standard', 'auto'):
        return {}, assumptions, ['Нет подтверждённого тарифа режима: ' + str(tier)]

    if model in TEXT_RATES:
        inp, out = count(units.get('input_tokens')), count(units.get('output_tokens'))
        if inp is None and out is None:
            return {}, assumptions, ['Нет наблюдаемого количества входных и выходных токенов.']
        cached, written = count(units.get('cached_tokens')), count(units.get('cache_write_tokens'))
        if cached is None:
            cached = 0; assumptions.append('Нет разбиения кеша: неизвестное чтение оценено как обычный вход.')
        if written is None:
            written = 0; assumptions.append('Нет разбиения кеша: неизвестная запись оценена как обычный вход.')
        prices = [Decimal(p) * factor for p in TEXT_RATES[model]]
        ttl = (call.get('pricing_parameters') or {}).get('cache_ttl')
        if model == 'gpt-6-luna' and written:
            if ttl == '12h': prices[2] *= 2
            elif ttl not in (None, '30m'):
                missing.append('Нет тарифа TTL кеша'); prices[2] = None
            elif ttl is None: assumptions.append('TTL записи кеша неизвестен: оценка по 30m.')
        if model == 'gpt-6-luna' and inp is not None and inp > 272000:
            prices = [p * Decimal('1.5' if i == 3 else '2') if p is not None else None for i,p in enumerate(prices)]
        if inp is not None and cached + written > inp:
            return {}, assumptions, ['Разбиение кеша превышает входные токены.']
        for name, qty, price in zip(['uncached_tokens', 'cached_tokens', 'cache_write_tokens', 'output_tokens'],
                [None if inp is None else inp - cached - written, cached, written, out], prices):
            if name == 'cache_write_tokens' and model != 'gpt-6-luna' and written:
                missing.append('Нет тарифа записи кеша этой модели'); continue
            if price is not None: add(name, qty, price)
    elif model in AUDIO_RATES:
        # Modalities have different rates; reasoning is already in the output total.
        incoming = usage.get('input_tokens_details') or usage.get('input_token_details') or usage.get('prompt_tokens_details') or {}
        outgoing = usage.get('output_tokens_details') or usage.get('output_token_details') or usage.get('completion_tokens_details') or {}
        cached = count(units.get('cached_tokens'))
        if cached is None:
            cached = count(incoming.get('cached_tokens'))
        if cached:
            return {}, assumptions, ['Для аудио с кешем нет подтверждённого разбиения цены по модальностям.']
        if factor != 1: return {}, assumptions, ['Нет подтверждённого тарифа аудио для выбранного режима.']
        for category, rate in AUDIO_RATES[model].items():
            direction, modality, _ = category.split('_')
            detail = incoming if direction == 'input' else outgoing
            qty = count(units.get(category))
            if qty is None: qty = count(detail.get(modality + '_tokens'))
            add(category, qty, rate)
    elif model in ('gpt-transcribe', 'gpt-live-1'):
        add('audio_seconds', usage.get('seconds'), '0.0045' if model == 'gpt-transcribe' else '0.05', 60)
        if model == 'gpt-live-1': missing.append('Backend-модель и инструменты GPT-Live оплачиваются отдельно.')
    else:
        missing.append('Нет проверенного тарифа для модели: ' + str(model or 'неизвестна'))

    web = count(call.get('billed_web_requests'))
    if web is None: web = count(units.get('web_requests'))
    if web is not None and web > 0:
        tool_types = (call.get('pricing_parameters') or {}).get('web_tool_types') or []
        preview = any(t.startswith('web_search_preview') for t in tool_types)
        add('web_requests', web, '0.025' if preview and model in ('gpt-4.1-mini', 'gpt-4.1-mini-2025-04-14') else '0.01', 1)
        if not tool_types: assumptions.append('Тип веб-поиска неизвестен: использован обычный тариф поиска.')
        if model in ('gpt-4.1-mini', 'gpt-4.1-mini-2025-04-14'):
            missing.append('Не подтверждён режим поисковых content tokens; возможна отдельная плата.')
    elif web is None and call.get('web_usage_expected'):
        missing.append('Нет числа оплаченных поисков; веб-действия не подставлены вместо него.')
    if not call.get('attempts_complete'):
        missing.append('Потребление всех HTTP-попыток подтверждено не полностью.')
    if call.get('conflicting_fields'):
        return {}, assumptions, ['Противоречивые журналы: ' + ', '.join(call['conflicting_fields'])]
    return components, assumptions, missing


def estimate_with_attempts(call):
    components, assumptions, missing = estimate_components(call)
    for n, attempt in enumerate((call.get('attempts') or [])[:-1], 1):
        if not attempt.get('usage'):
            missing.append('Нет usage предыдущей HTTP-попытки ' + str(n)); continue
        previous = dict(call, usage=attempt['usage'], units=consumption(attempt['usage']),
                        tool_usage=attempt.get('tool_usage'), attempts_complete=True,
                        billed_web_requests=billed_searches(attempt.get('tool_usage')))
        priced, extra_assumptions, gaps = estimate_components(previous)
        for key, value in priced.items():
            category = f'attempt_{n}:{key}'
            components[category] = dict(value, category=category)
        assumptions.extend(extra_assumptions); missing.extend(gaps)
    return components, assumptions, missing


def effective_cost(call):
    """Each category is either actual OR estimated. Recompute on every export."""
    with localcontext() as ctx:
        ctx.prec = 80
        actual = [dict(c, kind='actual') for c in call.get('allocation_categories', [])]
        if call.get('allocation_complete') and call.get('allocated_costs'):
            estimate, assumptions, missing = {}, [], []
        else:
            estimate, assumptions, missing = estimate_with_attempts(call)
        covered = {c['category'] for c in actual}
        # Do not add a modality estimate on top of an undivided token allocation.
        if covered & {'uncached_tokens', 'output_tokens', 'cached_tokens', 'cache_write_tokens'} and call.get('model') in AUDIO_RATES:
            estimate = {}; missing.append('Частичный факт аудио нельзя совместить с иной размерностью тарифа.')
        components = actual + [v for k,v in estimate.items() if k not in covered]
        # Older compatible callers may have a complete allocation without category rows.
        if call.get('allocation_complete') and not actual:
            components = [{'category': 'complete_call', 'kind': 'actual', 'currency': c, 'amount': a}
                          for c,a in call.get('allocated_costs', {}).items()]
        facts = defaultdict(Decimal); estimates = defaultdict(Decimal)
        for c in components:
            (facts if c['kind'] == 'actual' else estimates)[c['currency']] += Decimal(c['amount'])
        # Zero cache partitions do not turn an otherwise unknown model into a price.
        known = bool(components)
        kind = 'mixed' if facts and estimates else 'actual' if facts else 'estimated' if estimates else 'unknown'
        def amounts(d): return {k: money_text(v) for k,v in d.items()}
        total = {c: money_text(facts.get(c, 0) + estimates.get(c, 0)) for c in facts.keys() | estimates.keys()} if known else None
        return {'status': kind, 'label': LABELS[kind], 'amount': total,
                'actual': amounts(facts), 'estimated': amounts(estimates),
                'complete': known and not missing, 'components': components,
                'assumptions': list(dict.fromkeys(assumptions)), 'missing': list(dict.fromkeys(missing)),
                'pricing_version': PRICE_VERSION if estimates else None, 'source_url': PRICE_URL if estimates else None,
                'note': 'Факт — доля Costs API. Оценка — наблюдаемое потребление × публичный тариф; без скидок, налогов и кредитов.'}


def summarize(calls):
    with localcontext() as ctx:
        ctx.prec = 80
        actual = defaultdict(Decimal); estimated = defaultdict(Decimal); count_known = 0; complete = 0
        for call in calls:
            value = call.get('effective_cost') or effective_cost(call)
            count_known += value['amount'] is not None; complete += value['complete']
            for target,key in [(actual,'actual'), (estimated,'estimated')]:
                for c,a in value[key].items(): target[c] += Decimal(a)
        kind = 'mixed' if actual and estimated else 'actual' if actual else 'estimated' if estimated else 'unknown'
        currencies = actual.keys() | estimated.keys()
        return {'status': kind, 'label': LABELS[kind], 'amount': {c: money_text(actual.get(c, 0) + estimated.get(c, 0)) for c in currencies} if count_known else None,
                'actual': {c: money_text(a) for c,a in actual.items()}, 'estimated': {c: money_text(a) for c,a in estimated.items()},
                'known_calls': count_known, 'complete_calls': complete, 'calls': len(calls),
                'complete': bool(calls) and complete == len(calls)}

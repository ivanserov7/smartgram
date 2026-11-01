"""Observed provider consumption, never tariff estimates or counts of visible tool actions."""
import math


def count(value):
    return value if type(value) is int and value >= 0 else None


def object_value(value):
    return value if isinstance(value,dict) else {}


def consumption(usage):
    usage=object_value(usage)
    incoming=object_value(usage.get('input_tokens_details') or usage.get('input_token_details'))
    outgoing=object_value(usage.get('output_tokens_details') or usage.get('output_token_details'))
    total,cached,write=[count(v) for v in (usage.get('input_tokens'),incoming.get('cached_tokens'),incoming.get('cache_write_tokens'))]
    uncached=count(usage.get('input_uncached_tokens',incoming.get('uncached_tokens')))
    if uncached is None and all(v is not None for v in (total,cached,write)) and total>=cached+write:
        uncached=total-cached-write
    result={'input_tokens':total,'output_tokens':count(usage.get('output_tokens')),'cached_tokens':cached,
            'cache_write_tokens':write,'uncached_tokens':uncached}
    for direction,details in [('input',incoming),('output',outgoing)]:
        for modality in ['text','audio','image']:
            key=f'{direction}_{modality}_tokens'
            result[key]=count(usage.get(key,details.get(modality+'_tokens')))
    result['reasoning_tokens']=count(outgoing.get('reasoning_tokens'))  # Already included in output.
    return result


def billed_searches(tool_usage):
    tool_usage=object_value(tool_usage)
    return count(object_value(tool_usage.get('web_search')).get('num_requests'))


def monetary_categories(call):
    """Positive/unknown consumption categories that prevent a partial sum becoming a total."""
    units=call.get('units') or {};required=[];unknown=[]
    for key in ['uncached_tokens','cached_tokens','cache_write_tokens','output_tokens']:
        value=units.get(key)
        if value is None:unknown.append(key)
        elif value>0:required.append(key)
    # Explicitly absent cache partitions can be resolved by a confirmed zero provider aggregate.
    web=call.get('billed_web_requests',units.get('web_requests'))
    if web is not None and web>0:required.append('web_requests')
    elif web is None and call.get('web_usage_expected'):unknown.append('web_requests')
    tool=object_value(call.get('tool_usage'))
    for family,value in tool.items():
        if family=='web_search':continue
        if isinstance(value,dict) and any(isinstance(v,(int,float)) and v>0 for v in value.values()):
            unknown.append('tool:'+family)
    if units.get('input_audio_tokens') or units.get('output_audio_tokens'):
        unknown.append('audio_token_categories')
    return required,unknown

# use sso session to authenticate and query cloudwatch logs
import json
import os
import redis

cache_endpoint = os.environ.get('REDIS_ENDPOINT')


def handler(event, context):
    # this GC centre_id
    this_centre_id = "data-metoffice-noaa-global-cache"
    cache_client = redis.Redis(host=cache_endpoint, port=6379, decode_responses=True)
    # get all keys
    metrics = {}
    all_keys = cache_client.keys("*wmo_wis2_gc_*")
    # sort the keys alphabetically
    all_keys.sort()
    vals = cache_client.mget(all_keys)
    # create a dictionary of metrics that can be formatted to OpenMetrics spec
    for key, val in zip(all_keys, vals):
        key_parts = key.split("|")
        metric_name = key_parts[-1]

        metric_ob = {}
        # set the type
        if 'total' in metric_name:
            metric_ob['type'] = f"# TYPE {metric_name} counter"
        elif 'status' in metric_name:
            metric_ob['type'] = f"# TYPE {metric_name} gauge"
        elif 'timestamp' in metric_name:
            metric_ob['type'] = f"# TYPE {metric_name} summary"
        else:
            raise ValueError(f"Unknown metric type {metric_name} for {key}")
        if not metrics.get(metric_name):
            metrics[metric_name] = {}
            metrics[metric_name]['set'] = []
            metrics[metric_name]['type'] = metric_ob['type']
        # set the centre_id
        metric_ob['centre_id'] = key_parts[0]
        # set dataserver if it exists
        if len(key_parts) > 2:
            metric_ob['dataserver'] = key_parts[1]
        # set the value
        metric_ob['value'] = int(val)
        metrics_line = ""
        labels = f"{metric_name}{{centre_id=\"{metric_ob['centre_id']}\""
        if metric_ob.get('dataserver'):
            labels += f",dataserver=\"{metric_ob['dataserver']}\""
        labels += ",report_by=\"data-metoffice-noaa-global-cache\""
        labels += "}"
        metrics_line += f"{labels} {metric_ob['value']}"
        metrics[metric_name]['set'].append(metrics_line)

    metrics_output = []
    for i, k in enumerate(metrics):
        metric_type, metric_family = k, metrics[k]
        # append to output
        metrics_output.append(metric_family['type'])
        # metrics_output += f"{metric_family['type']}"
        for indv_metric in metric_family['set']:
            # metrics_output += indv_metric
            metrics_output.append(indv_metric)
    # get all values
    return {
        'statusCode': 200,
        'body': "\n".join(metrics_output),
        'headers': {'content-type': 'text/plain'}
    }
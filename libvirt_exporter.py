from __future__ import print_function
import sys
import argparse
import libvirt
import sched
import time
import prometheus_client
from prometheus_client import Gauge
from xml.etree import ElementTree

parser = argparse.ArgumentParser(description='libvirt_exporter scrapes domains metrics from libvirt daemon')
parser.add_argument('-si', '--scrape_interval', help='scrape interval for metrics in seconds', default=5)
parser.add_argument('-uri', '--uniform_resource_identifier', help='Libvirt Uniform Resource Identifier',
                    default="qemu:///system")
args = vars(parser.parse_args())
uri = args["uniform_resource_identifier"]

last_values = {}


def connect_to_uri(qemu_uri):
    conn = libvirt.open(qemu_uri)

    if conn is None:
        print('Failed to open connection to ' + qemu_uri, file=sys.stderr)
    else:
        print('Successfully connected to ' + qemu_uri)
    return conn


def get_domains(conn):
    domains = []

    for dom_id in conn.listDomainsID():
        dom = conn.lookupByID(dom_id)

        if dom is None:
            print('Failed to find the domain ' + dom.UUIDString(), file=sys.stderr)
        else:
            domains.append(dom)

    if len(domains) == 0:
        print('No running domains in URI')
        return None
    else:
        return domains


def get_labels(dom):
    tree = ElementTree.fromstring(dom.XMLDesc())
    if tree.find('metadata'):
        ns = {'nova': 'http://openstack.org/xmlns/libvirt/nova/1.0'}
        instance_name = tree.find('metadata').find('nova:instance', ns).find('nova:name', ns).text
        project_name = tree.find('metadata').find('nova:instance', ns).\
            find('nova:owner', ns).find('nova:project', ns).text
        labels = {'domain': instance_name, 'uuid': dom.UUIDString(), 'project_name': project_name}
    else:
        instance_name = tree.find('name').text
        labels = {'domain': instance_name, 'uuid': dom.UUIDString()}
    return labels


def get_metrics_collections(metric_names, labels, stats):
    dimensions = []
    metrics_collection = {}

    for mn in metric_names:
        if type(stats) is list:
            dimensions = [[stats[0][mn], labels]]
        elif type(stats) is dict:
            dimensions = [[stats[mn], labels]]
        metrics_collection[mn] = dimensions

    return metrics_collection


def get_metrics_multidim_collections(dom, device, **kwargs):
    tree = ElementTree.fromstring(dom.XMLDesc())
    targets = []

    # for target in tree.findall("devices/" + device + "/target"):  # !
    if device == 'disk':
        for target in tree.findall("devices/" + device + "[@device='disk']/target"):
            targets.append(target.get("dev"))
    else:
        for target in tree.findall("devices/" + device + "/target"):
            targets.append(target.get("dev"))

    all_metrics_collection = []

    for target in targets:
        metrics_collection = {}
        stats = []
        metric_names = []
        labels = get_labels(dom)
        if device == "disk":
            labels['target_disk'] = target
            if 'metric_names' in kwargs.keys():
                stats = dom.blockInfo(target)
                metric_names += kwargs['metric_names']
            else:
                disk_stats = dom.blockStatsFlags(target)
                metric_names += disk_stats.keys()
                stats = disk_stats.values()

        elif device == "interface":
            labels['target_interface'] = target
            stats = dom.interfaceStats(target)
            metric_names += kwargs['metric_names']

        for mn in metric_names:
            dimensions = []
            stats_af = dict(zip(metric_names, stats))
            dimension = [stats_af[mn], labels]
            dimensions.append(dimension)
            metrics_collection[mn] = dimensions
        all_metrics_collection.append(metrics_collection)
    return all_metrics_collection


def custom_derivative(new, time_delta=True, interval=15,
                      allow_negative=False, instance=None):
    """
    Calculate the derivative of the metric.
    """
    # Format Metric Path
    path = instance

    if path in last_values:
        old = last_values[path]
        # Check for rollover
        if new < old:
            # old = old - max_value
            # Store Old Value
            last_values[path] = new
            # Return 0 if instance was rebooted
            return 0
        # Get Change in X (value)
        derivative_x = new - old

        # If we pass in a interval, use it rather then the configured one
        interval = float(interval)

        # Get Change in Y (time)
        if time_delta:
            derivative_y = interval
        else:
            derivative_y = 1

        result = float(derivative_x) / float(derivative_y)
        if result < 0 and not allow_negative:
            result = 0
    else:
        result = 0

    # Store Old Value
    last_values[path] = new

    # Return result
    return result


def add_metrics(dom, header_mn, g_dict):
    labels = get_labels(dom)
    metrics_collection = []
    unit = ''

    if header_mn == "libvirt_cpu_stats_":

        vcpus = dom.getCPUStats(True, 0)
        totalcpu = 0
        for vcpu in vcpus:
            cputime = vcpu['cpu_time']
            totalcpu += cputime

        value = float(totalcpu / dom.maxVcpus()) / 10000000.0
        cpu_percent = custom_derivative(new=value, instance=dom.UUIDString())

        # metric_names = stats[0].keys()
        stats = [{'cpu_used': cpu_percent, 'max_cpu': dom.maxVcpus()}]
        metric_names = ['cpu_used', 'max_cpu']
        metrics_cpu = get_metrics_collections(metric_names, labels, stats)
        metrics_collection.append(metrics_cpu)
        unit = ""

    elif header_mn == "libvirt_mem_stats_":
        stats = dom.memoryStats()
        metric_names = stats.keys()
        metrics_mem = get_metrics_collections(metric_names, labels, stats)
        metrics_collection.append(metrics_mem)
        unit = ""

    elif header_mn == "libvirt_block_stats_":

        metrics_disk = get_metrics_multidim_collections(dom, device="disk")
        metrics_collection += metrics_disk
        unit = ""

    elif header_mn == "libvirt_disk_":

        metric_names = ['capacity',
                        'allocation',
                        'physical']
        metrics_interface = get_metrics_multidim_collections(dom, device="disk",
                                                             metric_names=metric_names)
        metrics_collection += metrics_interface

        unit = ""

    elif header_mn == "libvirt_interface_":

        metric_names = ['receive_bytes',
                        'receive_packets',
                        'receive_errors',
                        'receive_drops',
                        'transmit_bytes',
                        'transmit_packets',
                        'transmit_errors',
                        'transmit_drops']
        metrics_interface = get_metrics_multidim_collections(dom, device="interface",
                                                             metric_names=metric_names)
        metrics_collection += metrics_interface

        unit = ""

    if metrics_collection:
        for metrics_dev in metrics_collection:
            for mn in metrics_dev:
                metric_name = header_mn + mn + unit
                dimensions = metrics_dev[mn]

                if metric_name not in g_dict.keys():

                    metric_help = 'help'
                    labels_names = metrics_dev[mn][0][1].keys()

                    g_dict[metric_name] = Gauge(metric_name, metric_help, labels_names)

                    for dimension in dimensions:
                        dimension_metric_value = dimension[0]
                        dimension_label_values = dimension[1].values()
                        g_dict[metric_name].labels(*dimension_label_values).set(dimension_metric_value)
                else:
                    for dimension in dimensions:
                        dimension_metric_value = dimension[0]
                        dimension_label_values = dimension[1].values()
                        g_dict[metric_name].labels(*dimension_label_values).set(dimension_metric_value)
    return g_dict


def job(qemu_uri, g_dict, scheduler):
    print('BEGIN JOB :', time.time())
    conn = connect_to_uri(qemu_uri)
    domains = get_domains(conn)
    while domains is None:
        domains = get_domains(conn)
        time.sleep(int(args["scrape_interval"]))

    for dom in domains:

        print(dom.UUIDString())

        headers_mn = ["libvirt_cpu_stats_", "libvirt_mem_stats_",
                      "libvirt_block_stats_", "libvirt_interface_",
                      "libvirt_disk_"]

        for header_mn in headers_mn:
            g_dict = add_metrics(dom, header_mn, g_dict)

    conn.close()
    print('FINISH JOB :', time.time())
    scheduler.enter((int(args["scrape_interval"])), 1, job, (qemu_uri, g_dict, scheduler))


def main():
    prometheus_client.start_http_server(9177)

    g_dict = {}

    scheduler = sched.scheduler(time.time, time.sleep)
    print('START:', time.time())
    scheduler.enter(0, 1, job, (uri, g_dict, scheduler))
    scheduler.run()


if __name__ == '__main__':
    main()

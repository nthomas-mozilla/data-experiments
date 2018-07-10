import json
import logging
import os
import re
import sys
from collections import defaultdict

import arrow
import taskcluster


class InstanceTime:
    def __init__(self, graphId, use_hint='Unspecified', include=None, exclude=None,
                 multipliers=None):
        self.graphId = graphId
        self.use_hint = use_hint
        self.include = include
        self.exclude = exclude
        self.multipliers = multipliers or {}
        self.instance_time = defaultdict(int)

        self.graph = self.get_graph()
        self.get_usage()

    def get_graph(self):
        graph_file = os.path.join(CACHE, self.graphId)
        if os.path.exists(graph_file):
            log.debug('Reusing cached graph {} for {}'.format(self.graphId, self.use_hint))
            graph = json.load(open(graph_file))
        else:
            log.info('Retrieving task graph {} for {}'.format(self.graphId, self.use_hint))
            graph = []

            def pagination(y):
                log.info('Adding another {} tasks'.format(len(y.get('tasks', []))))
                graph.extend(y.get('tasks', []))

            queue.listTaskGroup(self.graphId, paginationHandler=pagination)
            with open(graph_file, 'w') as f:
                json.dump(graph, f, indent=4, sort_keys=True)

        return graph

    def get_multiplier(self, label):
        # Return the mulitplier for the first prefix which matches on the task label
        for prefix, multiplier in self.multipliers.items():
            if label.startswith(prefix):
                return multiplier
        # otherwise default to unity
        return 1

    def get_usage(self):
        log.info('Finding usage for {}'.format(self.use_hint))
        if not self.include and not self.exclude:
            log.error('Please specify at least one of include and exclude')
            sys.exit(1)

        for task in self.graph:
            taskId = task['status']['taskId']
            label = task['task']['metadata']['name']
            worker = task['status']['workerType']

            if self.exclude and any([re.match(r, label) for r in self.exclude]):
                log.debug('EXCLUDE {} {} {}'.format(taskId, label, worker))

            # count time for jobs if we have an include and hit it, or if we only have an exclude
            # and want to catch everything else
            elif (self.include and any([re.match(r, label) for r in self.include])) or \
                    (self.exclude and not self.include):
                run = task['status']['runs'][-1]
                if run['state'] != 'completed':
                    log.warn('{} is in state {} instead of completed, ignoring'.format(
                        label, run['state']))
                    continue
                elapsed = arrow.get(run['resolved']) - arrow.get(run['started'])
                multiplier = self.get_multiplier(label)
                self.instance_time[worker] += elapsed.total_seconds() * multiplier
                log.debug('INCLUDE {} {} {} m={} t={}'.format(taskId, label, worker,
                                                              multiplier, elapsed.total_seconds()))
            else:
                log.debug('UNKNOWN {} {} {}'.format(taskId, label, worker))

    def print_usage(self):
        print("Data for {} (seconds):".format(self.use_hint))
        for worker, workerTime in self.instance_time.items():
            print('{:30s} {:-10.0f}'.format(worker, workerTime))


logging.basicConfig(level=logging.INFO, format='%(message)s')
log = logging.getLogger(__name__)

CACHE = os.path.abspath('cache')
if not os.path.exists(CACHE):
    os.makedirs(CACHE)

queue = taskcluster.Queue()

"""
We're looking at DevEdition 62.0b6, and want to calculate the total instance-time of work
we would not do if we were repacking Beta instead. So en-US nightlies, the release automation,
and possibly the tests. Later we'll estimate what a repack would use.
"""
# on-push graph: SC__1kWXR4esb499TFHyPQ
# we do nightly + sign + repackage + repackage-sign on push
onpush_build_time = InstanceTime(
    'SC__1kWXR4esb499TFHyPQ',
    'on-push compile',
    exclude=['^test.*'],            # ignore all tests
    include=['.*-devedition-.*'],   # to weed out firefox builds
)

onpush_all_tests_time = InstanceTime(
    'SC__1kWXR4esb499TFHyPQ',
    'on-push all deved tests',
    include=['^test-.*devedition'],
)

promote_time = InstanceTime(
    'N5fSRkSZQsOteqDPo8tOJA',
    'release promote',
    exclude=[   # most of jobs will go away, but we keep ...
        '^release-bouncer-sub-devedition',
        '^release-early-tagging-devedition',
        '^release-generate-checksums-devedition-.*',
        '^release-notify-promote-devedition',
        '^release-source-.*',
        '^release-update-verify-config-devedition-.*',   # maybe not these ?
        '^release-update-verify-devedition-.*',
        '^sign-and-push-langpacks-.*',              # maybe just the AMO push and reuse Beta xpi ?
    ],
)

push_time = InstanceTime(
    'H3QyLn2HRxixA3cw1ulgew',
    'release push',
    exclude=['.*'],  # still need to push to releases, bouncer, final verify, notify
)

# ship: a13b0N6gSE-eCKVo4UOXig
ship_time = InstanceTime(
    'a13b0N6gSE-eCKVo4UOXig',
    'release ship',
    include=[  # still do bouncer alias, version bump (really tagging)
        'release-balrog-scheduling-devedition',
        'release-mark-as-shipped-devedition',
        'release-notify-ship-devedition',
    ],
)

"""
Now lets look at the Firefox 61.0.1 build to see how long it takes to do EME-free on Mac and
Windows, as an estimate for how long a devtools repack might take. EME-free because it's the only
repack we do for all locales, but not on Linux so there's a hacky multiplier applied to jobs on
Mac that are also done on Linux. Filesizes are most similar on those two platforms.

All other work we'd need to do, like generate checksums, modify bouncer, etc, has been left into
the analysis above and doesn't to be added again here.
"""
eme_free_repack_time = InstanceTime(
    'DgylP9ewT_2-SLCclTex0A',
    'release EME-free repack',
    include=[
        '^release-eme-free-.*',
    ],
    multipliers={
        # task name prefix: multiplier value
        'release-eme-free-repack-macosx64-nightly': 3,  # tar.bz2 repacking may be slower than .zip
        'release-eme-free-repack-repackage-signing-macosx64': 3,    # gpg signing
        'release-eme-free-repack-beetmover-macosx64': 3,            # move to candidates
        'release-eme-free-repack-beetmover-checksums-macosx64': 3,  # for SHA256SUMS et al.
    },
)

# time to output the results
print('')
print('Time spent on full build:')
print('-------------------------')
for data in [onpush_build_time, onpush_all_tests_time,
             promote_time, push_time, ship_time]:
    data.print_usage()
    print('')

print('Estimated time spent by repacking:')
print('----------------------------------')
for data in [eme_free_repack_time]:
    data.print_usage()
    print('')

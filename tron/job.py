import logging
from collections import deque

from tron import action
from tron.utils import timeutils

log = logging.getLogger('tron.job')

RUN_LIMIT = 50

class JobRun(object):
    def __init__(self, job, prev=None, data=None):
        self.run_num = job.next_num()
        self.job = job
        self.prev = prev
        self.next = None
        
        self.run_time = None
        self.start_time = None
        self.end_time = None
        self.node = None
        
        self.runs = []
        self.data = {'runs':[], 'run_time':None, 'start_time': None, 'end_time': None, 'run_num': self.run_num}
               
    def set_run_time(self, run_time):
        self.run_time = run_time
        self.data['run_time'] = run_time

        for r in self.runs:
            if not r.required_runs:
                r.run_time = run_time

    def scheduled_start(self):
        self.attempt_start()

        if self.is_scheduled:
            if self.job.queueing:
                log.warning("Previous job, %s, not finished - placing in queue", self.job.name)
                self.queue()
            else:
                log.warning("Previous job, %s, not finished - cancelling instance", self.job.name)
                self.cancel()

    def start(self):
        log.info("Starting action job %s", self.job.name)
        self.start_time = timeutils.current_time()
        self.data['start_time'] = self.start_time

        for r in self.runs:
            r.attempt_start()
    
    def attempt_start(self):
        if self.should_start:
            self.start()

    def last_success_check(self):
        if not self.job.last_success or self.run_num > self.job.last_success.run_num:
            self.job.last_success = self

    def run_completed(self):
        if self.is_success:
            self.last_success_check()
            
            if self.job.constant and self.job.running:
                self.job.build_run(self).start()

        if not self.is_running:
            self.end_time = timeutils.current_time()
            self.data['end_time'] = self.end_time
            
            next = self.next
            while next and next.is_cancelled:
                next = next.next
           
            if next and next.is_queued:
                next.attempt_start()

    def state_changed(self):
        self.data['run_time'] = self.run_time
        self.data['start_time'] = self.start_time
        self.data['end_time'] = self.end_time

        if self.job.state_callback:
            self.job.state_callback()

    def schedule(self):
        for r in self.runs:
            r.schedule()

    def queue(self):
        for r in self.runs:
            r.queue()
        
    def cancel(self):
        for r in self.runs:
            r.cancel()
    
    def succeed(self):
        for r in self.runs:
            r.mark_success()

    def fail(self):
        for r in self.runs:
            r.fail(0)

    @property
    def id(self):
        return "%s.%s" % (self.job.name, self.run_num)
    
    @property
    def is_failed(self):
        return any([r.is_failed for r in self.runs])

    @property
    def is_success(self):
        return all([r.is_success for r in self.runs])

    @property
    def is_queued(self):
        return all([r.is_queued for r in self.runs])
   
    @property
    def is_running(self):
        return any([r.is_running for r in self.runs])

    @property
    def is_scheduled(self):
        return any([r.is_scheduled for r in self.runs])

    @property
    def is_unknown(self):
        return any([r.is_unknown for r in self.runs])

    @property
    def is_cancelled(self):
        return all([r.is_cancelled for r in self.runs])

    @property
    def should_start(self):
        if not self.job.running or not (self.is_scheduled or self.is_queued):
            return False

        prev_job = self.prev
        while prev_job and prev_job.is_cancelled:
            prev_job = prev_job.prev

        return not (prev_job and (prev_job.is_running or prev_job.is_queued or prev_job.is_scheduled))

class Job(object):
    run_num = 0
    def next_num(self):
        self.run_num += 1
        return self.run_num - 1

    def __init__(self, name=None, action=None):
        self.name = name
        self.topo_actions = [action] if action else []
        self.scheduler = None
        self.runs = deque()
        self.data = deque()
        
        self.queueing = False
        self.running = True
        self.constant = False
        self.last_success = None
        
        self.state_callback = None
        self.run_limit = RUN_LIMIT
        self.state_callback = None
        self.node_pool = None
        self.output_dir = None
        
    def next_to_run(self):
        def choose(prev, next):
            return next if next.is_queued or next.is_scheduled else prev

        return reduce(choose, self.runs, None)

    def get_run_by_num(self, num):
        ind = self.runs[0].run_num - num
        return self.runs[ind] if ind in range(len(self.runs)) else None

    def remove_old_runs(self):
        """Remove old runs so the number left matches the run limit.
        However only removes runs up to the last success
        """
        while len(self.runs) > self.run_limit and self.last_success and self.runs[-1].run_num < self.last_success.run_num:
            old = self.runs.pop()
            self.data.pop()
            old.next.prev = None

    def next_run(self):
        if not self.scheduler or not self.running:
            return None
        
        job_run = self.scheduler.next_run(self)
        if job_run:
            for a in job_run.runs:
                a.state_changed()
            
        return job_run

    def build_run(self, prev=None):
        job_run = JobRun(self, prev)
        if self.node_pool:
            job_run.node = self.node_pool.next() 
        
        if prev:
            prev.next = job_run

        runs = {}
        for a in self.topo_actions:
            run = a.build_run(job_run)
            runs[a.name] = run
            
            job_run.runs.append(run)
            job_run.data['runs'].append(run.data)

            for req in a.required_actions:
                runs[req.name].waiting_runs.append(run)
                run.required_runs.append(runs[req.name])
     
        self.runs.appendleft(job_run)
        self.data.appendleft(job_run.data)
        self.remove_old_runs()

        return job_run

    def restore_run(self, data, prev=None):
        run = self.build_run(prev)
        for r, state in zip(run.runs, data['runs']):
            r.restore_state(state)
            
        run.run_num = data['run_num']
        run.start_time = data['start_time']
        run.end_time = data['end_time']
        run.set_run_time(data['run_time'])
        
        if run.is_success:
            self.last_success = run

        run.state_changed()
        return run


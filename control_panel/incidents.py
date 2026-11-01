"""Dashboard-only incident rules. No notifications or automatic restarts."""
import time

class Monitor:
    def __init__(self, thresholds=None):
        self.since = {}
        self.thresholds = dict(cpu_percent=90,memory_percent=90,disk_free_mb=1024,sustained_seconds=300)
        if thresholds:
            for key, value in thresholds.items():
                if key not in self.thresholds or not isinstance(value,(int,float)) or not 0 < value < 100000:
                    raise ValueError('Invalid incident threshold')
                if key.endswith('_percent') and value > 100:
                    raise ValueError('Percentage threshold must not exceed 100')
                self.thresholds[key] = value

    def sustained(self, key, active, seconds):
        if not active:
            self.since.pop(key,None)
            return False
        self.since.setdefault(key,time.time())
        return time.time()-self.since[key] >= seconds

    async def check(self, panel, services, host):
        rules = []
        async def rule(name,target,active,description,occurrence=''):
            rules.append((name,target,active,{'description':description},occurrence))
        for name, service in services.items():
            await rule('process_exit',name,service.get('expected_running',False) and service['ownership']!='owned',
                       'Managed process exited unexpectedly; no automatic restart is configured.',service.get('exit_code'))
        bot = services.get('bot',{})
        disconnected = bot.get('ownership')=='owned' and bot.get('state')!='Healthy'
        await rule('discord_connection','bot',self.sustained('discord',disconnected,60),
                   'Discord connection has been unhealthy for at least 60 seconds.')
        thresholds = self.thresholds
        pressure = (host['cpu_percent']>=thresholds['cpu_percent'] or host['memory_percent']>=thresholds['memory_percent']
                    or host['disk_free']<thresholds['disk_free_mb']*1024*1024)
        await rule('resource_pressure','host',self.sustained('resources',pressure,thresholds['sustained_seconds']),
                   'Host resource pressure exceeded the configured sustained threshold.')
        await rule('storage','panel',bool(panel.storage_error),'History storage could not be written.')
        total = sum(panel.dropped.values())
        previous = getattr(self,'dropped',0)
        if total > previous:
            await rule('telemetry_loss','panel',True,'Telemetry events were dropped.',total)
            self.since['last_drop'] = time.time()
        elif time.time()-self.since.get('last_drop',0)>60:
            await rule('telemetry_loss','panel',False,'No telemetry drops in the last minute.')
        self.dropped = total
        auth = await panel.db('recent_auth')
        if auth['rejected'] or auth['recovered']:
            await rule('wwm_authorization','wwm',auth['rejected'],'Three consecutive WWM authorization rejections; expiry is not established.',auth['latest'])
        heartbeat = bot.get('heartbeat') or {}
        for job in heartbeat.get('tasks',[]):
            if job.get('instrumented'):
                await rule('job_failure',job['name'],job.get('consecutive_failures',0)>=3,
                           'Registered job failed at least three consecutive iterations.',job.get('id',''))
        await panel.db('apply_incidents',rules)

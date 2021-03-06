#!/usr/bin/env python

import os
import imp
import json
import logging
import datetime

from multiprocessing import Process, Queue
import bottle
import prometheus_client as prom

try:
    import queue
except:
    import Queue as queue

mod = imp.load_source('function',
                      '/kubeless/%s.py' % os.getenv('MOD_NAME'))
func = getattr(mod, os.getenv('FUNC_HANDLER'))
func_port = os.getenv('FUNC_PORT', 8080)

timeout = float(os.getenv('FUNC_TIMEOUT', 180))

memfile_max = int(os.getenv('FUNC_MEMFILE_MAX', 100*1024*1024))
bottle.BaseRequest.MEMFILE_MAX = memfile_max

app = application = bottle.app()

func_hist = prom.Histogram('function_duration_seconds',
                           'Duration of user function in seconds',
                           ['method'])
func_calls = prom.Counter('function_calls_total',
                           'Number of calls to user function',
                          ['method'])
func_errors = prom.Counter('function_failures_total',
                           'Number of exceptions in user function',
                           ['method'])

function_context = {
    'function-name': func,
    'timeout': timeout,
    'runtime': os.getenv('FUNC_RUNTIME'),
    'memory-limit': os.getenv('FUNC_MEMORY_LIMIT'),
}

def funcWrap(q, event, c):
    try:
        q.put(func(event, c))
    except Exception as inst:
        q.put(inst)

@app.get('/healthz')
def healthz():
    return 'OK'

@app.get('/metrics')
def metrics():
    bottle.response.content_type = prom.CONTENT_TYPE_LATEST
    return prom.generate_latest(prom.REGISTRY)


@app.route('/<:re:.*>', method=['GET', 'POST', 'PATCH', 'DELETE'])
def handler():
    req = bottle.request
    content_type = req.get_header('content-type')
    data = req.body.read()
    if content_type == 'application/json':
        data = req.json
    event = {
        'data': data,
        'event-id': req.get_header('event-id'),
        'event-type': req.get_header('event-type'),
        'event-time': req.get_header('event-time'),
        'event-namespace': req.get_header('event-namespace'),
        'extensions': {
            'request': req
        }
    }
    method = req.method
    func_calls.labels(method).inc()
    with func_errors.labels(method).count_exceptions():
        with func_hist.labels(method).time():
            q = Queue()
            p = Process(target=funcWrap, args=(q, event, function_context))
            p.start()

            try:
                res = q.get(block=True, timeout=timeout)
            except queue.Empty:
                p.terminate()
                p.join()
                return bottle.HTTPError(408, "Timeout while processing the function")
            else:
                p.join()
                if isinstance(res, Exception) and not isinstance(res, bottle.HTTPResponse):
                    logging.error("Function returned an exception: %s", res)
                    raise res
                return res


if __name__ == '__main__':
    import sys
    import requestlogger
    loggedapp = requestlogger.WSGILogger(
        app,
        [logging.StreamHandler(stream=sys.stdout)],
        requestlogger.ApacheFormatter())
    bottle.run(loggedapp, server='cherrypy', host='0.0.0.0', port=func_port)

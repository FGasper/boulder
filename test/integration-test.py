#!/usr/bin/env python2.7
# -*- coding: utf-8 -*-
"""
This file contains basic infrastructure for running the integration test cases.
Most test cases are in v1_integration.py and v2_integration.py. There are a few
exceptions: Test cases that don't test either the v1 or v2 API are in this file,
and test cases that have to run at a specific point in the cycle (e.g. after all
other test cases) are also in this file.
"""
import argparse
import atexit
import datetime
import inspect
import json
import os
import random
import re
import requests
import subprocess
import signal
import time

import startservers

import chisel
from chisel import auth_and_issue
import v1_integration
import v2_integration
from helpers import *

from acme import challenges

def run_client_tests():
    root = os.environ.get("CERTBOT_PATH")
    assert root is not None, (
        "Please set CERTBOT_PATH env variable to point at "
        "initialized (virtualenv) client repo root")
    cmd = os.path.join(root, 'tests', 'boulder-integration.sh')
    run(cmd, cwd=root)

def run_expired_authz_purger():
    # Note: This test must be run after all other tests that depend on
    # authorizations added to the database during setup
    # (e.g. test_expired_authzs_404).

    def expect(target_time, num, table):
        tool = "expired-authz-purger2"
        out = get_future_output("./bin/expired-authz-purger2 --single-run --config cmd/expired-authz-purger2/config.json", target_time)
        if 'via FAKECLOCK' not in out:
            raise Exception("%s was not built with `integration` build tag" % (tool))
        if num is None:
            return
        expected_output = 'deleted %d expired authorizations' % (num)
        if expected_output not in out:
            raise Exception("%s did not print '%s'.  Output:\n%s" % (
                  tool, expected_output, out))

    now = datetime.datetime.utcnow()

    # Run the purger once to clear out any backlog so we have a clean slate.
    expect(now+datetime.timedelta(days=+365), None, "")

    # Make an authz, but don't attempt its challenges.
    chisel.make_client().request_domain_challenges("eap-test.com")

    # Run the authz twice: Once immediate, expecting nothing to be purged, and
    # once as if it were the future, expecting one purged authz.
    after_grace_period = now + datetime.timedelta(days=+14, minutes=+3)
    expect(now, 0, "pendingAuthorizations")
    expect(after_grace_period, 1, "pendingAuthorizations")

    auth_and_issue([random_domain()])
    after_grace_period = now + datetime.timedelta(days=+67, minutes=+3)
    expect(now, 0, "authz")
    expect(after_grace_period, 1, "authz")

def run_janitor():
    # Set the fake clock to a year in the future such that all of the database
    # rows created during the integration tests are older than the grace period.
    now = datetime.datetime.utcnow()
    target_time = now+datetime.timedelta(days=+365)

    e = os.environ.copy()
    e.setdefault("GORACE", "halt_on_error=1")
    e.setdefault("FAKECLOCK", fakeclock(target_time))

    # Note: Must use exec here so that killing this process kills the command.
    cmdline = "exec ./bin/boulder-janitor --config test/config-next/janitor.json"
    p = subprocess.Popen(cmdline, shell=True, env=e)

    # Wait for the janitor to come up
    waitport(8014, "boulder-janitor", None)

    def statline(statname, table):
        # NOTE: we omit the trailing "}}" to make this match general enough to
        # permit new labels in the future.
        return "janitor_{0}{{table=\"{1}\"".format(statname, table)

    def get_stat_line(port, stat):
        url = "http://localhost:%d/metrics" % port
        response = requests.get(url)
        for l in response.content.split("\n"):
            if l.strip().startswith(stat):
                return l
        return None

    def stat_value(line):
        parts = line.split(" ")
        if len(parts) != 2:
            raise Exception("stat line {0} was missing required parts".format(line))
        return parts[1]

    # Wait for the janitor to report it isn't finding new work
    print("waiting for boulder-janitor work to complete...\n")
    workDone = False
    for i in range(10):
        certStatusWorkbatch = get_stat_line(8014, statline("workbatch", "certificateStatus"))
        certsWorkBatch = get_stat_line(8014, statline("workbatch", "certificates"))
        certsPerNameWorkBatch = get_stat_line(8014, statline("workbatch", "certificatesPerName"))

        allReady = True
        for line in [certStatusWorkbatch, certsWorkBatch, certsPerNameWorkBatch]:
            if stat_value(line) != "0":
                allReady = False

        if allReady is False:
            print("not done after check {0}. Sleeping".format(i))
            time.sleep(2)
        else:
            workDone = True
            break

    if workDone is False:
        raise Exception("Timed out waiting for janitor to report all work completed\n")

    # Check deletion stats are not empty/zero
    for i in range(10):
        certStatusDeletes = get_stat_line(8014, statline("deletions", "certificateStatus"))
        certsDeletes = get_stat_line(8014, statline("deletions", "certificates"))
        certsPerNameDeletes = get_stat_line(8014, statline("deletions", "certificatesPerName"))

        if certStatusDeletes is None or certsDeletes is None or certsPerNameDeletes is None:
            print("delete stats not present after check {0}. Sleeping".format(i))
            time.sleep(2)
            continue

        for l in [certStatusDeletes, certsDeletes, certsPerNameDeletes]:
            if stat_value(l) == "0":
                raise Exception("Expected a non-zero number of deletes to be performed. Found {0}".format(l))

    # Check that all error stats are empty
    errorStats = [
      statline("errors", "certificateStatus"),
      statline("errors", "certificates"),
      statline("errors", "certificatesPerName"),
    ]
    for eStat in errorStats:
        actual = get_stat_line(8014, eStat)
        if actual is not None:
            raise Exception("Expected to find no error stat lines but found {0}\n".format(eStat))

    # Terminate the janitor
    p.terminate()

def test_single_ocsp():
    """Run the single-ocsp command, which is used to generate OCSP responses for
       intermediate certificates on a manual basis. Then start up an
       ocsp-responder configured to respond using the output of single-ocsp,
       check that it successfully answers OCSP requests, and shut the responder
       back down.

       This is a non-API test.
    """
    run("./bin/single-ocsp -issuer test/test-root.pem \
            -responder test/test-root.pem \
            -target test/test-ca2.pem \
            -pkcs11 test/test-root.key-pkcs11.json \
            -thisUpdate 2016-09-02T00:00:00Z \
            -nextUpdate 2020-09-02T00:00:00Z \
            -status 0 \
            -out /tmp/issuer-ocsp-responses.txt")

    p = subprocess.Popen(
        './bin/ocsp-responder --config test/issuer-ocsp-responder.json', shell=True)
    waitport(4003, './bin/ocsp-responder --config test/issuer-ocsp-responder.json')

    # Verify that the static OCSP responder, which answers with a
    # pre-signed, long-lived response for the CA cert, works.
    verify_ocsp("test/test-ca2.pem", "test/test-root.pem", "http://localhost:4003", "good")

    p.send_signal(signal.SIGTERM)
    p.wait()

def test_stats():
    """Fetch Prometheus metrics from a sample of Boulder components to check
       they are present.

       This is a non-API test.
    """
    def expect_stat(port, stat):
        url = "http://localhost:%d/metrics" % port
        response = requests.get(url)
        if not stat in response.content:
            print(response.content)
            raise Exception("%s not present in %s" % (stat, url))
    expect_stat(8000, "\nresponse_time_count{")
    expect_stat(8000, "\ngo_goroutines ")
    expect_stat(8000, '\ngrpc_client_handling_seconds_count{grpc_method="NewRegistration",grpc_service="ra.RegistrationAuthority",grpc_type="unary"} ')

    expect_stat(8002, '\ngrpc_server_handling_seconds_sum{grpc_method="PerformValidation",grpc_service="ra.RegistrationAuthority",grpc_type="unary"} ')

    expect_stat(8001, "\ngo_goroutines ")

exit_status = 1

def main():
    parser = argparse.ArgumentParser(description='Run integration tests')
    parser.add_argument('--certbot', dest='run_certbot', action='store_true',
                        help="run the certbot integration tests")
    parser.add_argument('--chisel', dest="run_chisel", action="store_true",
                        help="run integration tests using chisel")
    parser.add_argument('--filter', dest="test_case_filter", action="store",
                        help="Regex filter for test cases")
    # allow any ACME client to run custom command for integration
    # testing (without having to implement its own busy-wait loop)
    parser.add_argument('--custom', metavar="CMD", help="run custom command")
    parser.set_defaults(run_certbot=False, run_chisel=False,
        test_case_filter="", skip_setup=False)
    args = parser.parse_args()

    if not (args.run_certbot or args.run_chisel or args.run_loadtest or args.custom is not None):
        raise Exception("must run at least one of the letsencrypt or chisel tests with --certbot, --chisel, or --custom")

    if not args.test_case_filter:
        now = datetime.datetime.utcnow()

        six_months_ago = now+datetime.timedelta(days=-30*6)
        if not startservers.start(race_detection=True, fakeclock=fakeclock(six_months_ago)):
            raise Exception("startservers failed (mocking six months ago)")
        v1_integration.caa_client = caa_client = chisel.make_client()
        setup_six_months_ago()
        startservers.stop()

        twenty_days_ago = now+datetime.timedelta(days=-20)
        if not startservers.start(race_detection=True, fakeclock=fakeclock(twenty_days_ago)):
            raise Exception("startservers failed (mocking twenty days ago)")
        setup_twenty_days_ago()
        startservers.stop()

    if not startservers.start(race_detection=True):
        raise Exception("startservers failed")

    if args.run_chisel:
        run_chisel(args.test_case_filter)

    if args.run_certbot:
        run_client_tests()

    if args.custom:
        run(args.custom)

    # Skip the last-phase checks when the test case filter is one, because that
    # means we want to quickly iterate on a single test case.
    if not args.test_case_filter:
        run_cert_checker()
        check_balance()
        if not CONFIG_NEXT:
            run_expired_authz_purger()

        # Run the boulder-janitor. This should happen after all other tests because
        # it runs with the fake clock set to the future and deletes rows that may
        # otherwise be referenced by tests.
        run_janitor()

        # Run the load-generator last. run_loadtest will stop the
        # pebble-challtestsrv before running the load-generator and will not restart
        # it.
        run_loadtest()

    if not startservers.check():
        raise Exception("startservers.check failed")

    global exit_status
    exit_status = 0

def run_chisel(test_case_filter):
    for key, value in inspect.getmembers(v1_integration):
      if callable(value) and key.startswith('test_') and re.search(test_case_filter, key):
        value()
    for key, value in inspect.getmembers(v2_integration):
      if callable(value) and key.startswith('test_') and re.search(test_case_filter, key):
        value()
    for key, value in globals().items():
      if callable(value) and key.startswith('test_') and re.search(test_case_filter, key):
        value()

def run_loadtest():
    """Run the ACME v2 load generator."""
    latency_data_file = "%s/integration-test-latency.json" % tempdir

    # Stop the global pebble-challtestsrv - it will conflict with the
    # load-generator's internal challtestsrv. We don't restart it because
    # run_loadtest() is called last and there are no remaining tests to run that
    # might benefit from the pebble-challtestsrv being restarted.
    startservers.stopChallSrv()

    run("./bin/load-generator \
            -config test/load-generator/config/integration-test-config.json\
            -results %s" % latency_data_file)

def check_balance():
    """Verify that gRPC load balancing across backends is working correctly.

    Fetch metrics from each backend and ensure the grpc_server_handled_total
    metric is present, which means that backend handled at least one request.
    """
    addresses = [
        "sa1.boulder:8003",
        "sa2.boulder:8103",
        "publisher1.boulder:8009",
        "publisher2.boulder:8109",
        "va1.boulder:8004",
        "va2.boulder:8104",
        "ca1.boulder:8001",
        "ca2.boulder:8104",
        "ra1.boulder:8002",
        "ra2.boulder:8102",
    ]
    for address in addresses:
        metrics = requests.get("http://%s/metrics" % address)
        if not "grpc_server_handled_total" in metrics.text:
            raise Exception("no gRPC traffic processed by %s; load balancing problem?"
                % address)

def run_cert_checker():
    run("./bin/cert-checker -config %s/cert-checker.json" % default_config_dir)

if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        raise Exception("%s. Output:\n%s" % (e, e.output))

@atexit.register
def stop():
    if exit_status == 0:
        print("\n\nSUCCESS")
    else:
        print("\n\nFAILURE")

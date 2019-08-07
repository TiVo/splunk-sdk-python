#!/usr/bin/env python

from splunklib.searchcommands import dispatch, Configuration, Option, validators
from smartstreamingcommand import SmartStreamingCommand
import sys

# ------------------------------------------------------------
#
# Example Splunk SPL custom command that leverages new base class
# SmartStreamingCommand to demonstrate how to mitigate Splunk daemon
# timing issues to process large amounts of data (100s of millions of
# events or more).
#
# This example command does nothing, but simply echo back the input
# records it receives.
#
# ------------------------------------------------------------

@Configuration()
class EchoCommand(SmartStreamingCommand):

    throttleusec = Option(require=False, validate=validators.Integer())

    def stream(self, events):

        if not self.throttleusec is None:
            self.throttleMs = self.throttleusec / 1000.0

        for event in events:
            yield event


dispatch(EchoCommand, sys.argv, sys.stdin, sys.stdout, __name__)

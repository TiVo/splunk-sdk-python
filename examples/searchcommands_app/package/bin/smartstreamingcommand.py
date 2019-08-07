#!/usr/bin/env python

from splunklib.searchcommands import StreamingCommand
import sys
import select
import os
import gzip
import re
import csv
import time
import logging

try:
    from collections import OrderedDict  # must be python 2.7
except ImportError:
    from ..ordereddict import OrderedDict

from splunklib.six.moves import zip as izip
from splunklib.searchcommands.internals import (CsvDialect, MetadataDecoder)


class ChunkedInput(object):

    def __init__(self, infile, limit):
        self._file = infile
        self._limit = limit

    def __getattr__(self, name):
        return getattr(self._file, name)

    def __iter__(self):
        while True:
            if self._limit <= 0:
                return
            line = self._file.readline()
            yield line
            self._limit -= len(line)


class SmartStreamingCommand(StreamingCommand):
    """A smarter version of the Splunk SDK's StreamingCommand.

    Like the parent class, this class applies a transformation to
    search results as they travel through the streams pipeline.

    This class adds functionality that more intelligently reads events
    from the Splunk server, reducing the memory consumption of this
    custom command when it is running.  Additionally, this class adds
    functionality that will incrementally flush produced events, also
    reducing the memory footprint of this command.

    Finally, this class includes more careful handshaking between the
    custom command process and the parent Splunk daemon to avoid the
    "buffer full" Splunk daemon bug.  This includes always observing a
    "read one chunk, send one chunk" policy and ensuring that outbound
    chunks are never flushed at a rate faster than one event per
    "throttleMs" milliseconds.  The default for "throttleMs" is
    '0.08', meaning that standard batch of 50,000 events will not be
    flushed faster than once each four seconds.

    This class has been tested against the following configuration
    dimensions:

    - Single install Splunk server vs. SHC and indexer cluster (3x6)
    - On the searchhead (eg. after `localop`) vs. on indexers in parallel
    - With and without previews enabled
    - Against both generating and eventing base searches 

    This class otherwise supports the same functionality and interface
    as the parent, StreamingCommand, class.

    """

    def __init__(self):
        StreamingCommand.__init__(self)
        self._throttleMs = 0.08
        self._last_flush = None

    @property
    def throttleMs(self):
        return self._throttleMs

    @throttleMs.setter
    def throttleMs(self, value):
        self._throttleMs = value
        
    def stream(self, records):
        """ Generator function that processes and yields event records to the
        Splunk stream pipeline.

        You must override this method.

        """
        raise NotImplementedError('SmartStreamingCommand.stream(self, records)')

    # Override base class method to replace the record generator with
    # our own generator that understands how to stop after a chunk
    # without requiring the ifile to be closed...
    def _execute(self, ifile, process):
        self.logger.setLevel(logging.INFO)
        self._record_writer.write_records(self.stream(self._our_records(ifile)))
        self.finish()

    # Start reading a chunk by reading the header and returning the
    # metadata and body lengths.  The remainder of the chunk is not
    # read off of the provided file input.
    def _start_chunk(self, ifile):

        # noinspection PyBroadException
        try:
            header = ifile.readline()
        except Exception as error:
            raise RuntimeError('Failed to read transport header: {} : {}'.format(error,header))

        if header == "":
            ifile.close()
            return None, None

        if not header:
            return None, None

        match = SmartStreamingCommand._header.match(header)

        if match is None:
            raise RuntimeError('Failed to parse transport header: "{}"'.format(header))

        metadata_length, body_length = match.groups()
        metadata_length = int(metadata_length)
        body_length = int(body_length)

        return metadata_length, body_length

    _header = re.compile(r'chunked\s+1.0\s*,\s*(\d+)\s*,\s*(\d+)\s*\n')

    # Read and return the metadata from the provided file input.
    def _read_metadata(self, ifile, metadata_length):
                
        try:
            metadata = ifile.read(metadata_length)
        except Exception as error:
            raise RuntimeError('Failed to read metadata of length {}: {}'.format(metadata_length, error))

        decoder = MetadataDecoder()

        self.logger.info("Metadata: {}".format(metadata))
        try:
            metadata = decoder.decode(metadata)
        except Exception as error:
            raise RuntimeError('Failed to parse metadata of length {}: {}'.format(metadata_length, error))

        return metadata

    # Capture input events (of size bytes) from the provided file
    # input into a local, gzip'd file in the dispatch directory.
    def _capture_input(self, ifile, bytes):
    
        dispatch_dir = self._metadata.searchinfo.dispatch_dir
        if not os.path.exists(dispatch_dir):
            os.mkdir(dispatch_dir, 0775)
        
        file = 'input_snap_{}.gz'.format(os.getpid())
        path = os.path.join(dispatch_dir, file)
        self.logger.info('Capture input ({} bytes) in {}...'.format(bytes,file))
        
        with gzip.open(path, 'wb') as copy:
            for line in ChunkedInput(ifile, bytes):
                copy.write(line)
            copy.flush()
            copy.close()

        self._icopy_path = path
        self._icopy = gzip.open(path, 'rb')
        self._ifile = ifile

        self.logger.debug('Input captured')

    # Drain exactly one input chunk.
    def _drain_input_one_chunk(self, ifile):
        
        m_len, b_len = self._start_chunk(ifile)
        if m_len is not None and b_len is not None:
            try:
                ifile.read(m_len+b_len)
            except Exception as error:
                raise RuntimeError('Failed to clear chunk of lengths {} {}: {}'.format(m_len, b_len, error))
                    
    # Loop, checking the provided input file and, iff bytes are
    # present, read a chunk, until no bytes are present.
    def _drain_input(self, ifile):

        # Loop reading chunks out of the input until it is dry...
        chunks = 0
        check_input = not ifile.closed
        while check_input:

            check_input = False

            check_rd, check_wr, check_ex = select.select([ifile], [], [], 0)
            if check_rd == [ifile]:

                # Input available; drain it...
                self._drain_input_one_chunk(ifile)

                # Check again...
                check_input = not ifile.closed
                chunks += 1

        if chunks > 0:
            self.logger.info('Cleared {} input chunk(s)'.format(chunks))

    # Flush, but only at a certain rate (sleeps if called too often).
    def _gated_flush(self, count):

        if self._last_flush is None:
            self._last_flush = time.time()

        if count is 0:
            return
        
        intervalSec = self.throttleMs * count / 1000.0
        timeSec = time.time()

        # Check if we have flushed recently; iff so, stall briefly...
        if self._last_flush+intervalSec > timeSec:
            sleepSec = self._last_flush+intervalSec - timeSec
            self.logger.info('Sleep before flushing, {}s'.format(sleepSec))
            time.sleep(sleepSec)

        self.logger.info('Flushing events ({})...'.format(count))
        self.flush()
        self._last_flush = time.time()
        self.logger.debug('Flushed')

    # Generator function that captures input, then reads the captured
    # copy, yielding events in OrderedDict form.
    def _one_chunk_of_records(self, ifile):

        self._finished = True

        metadata_length, body_length = self._start_chunk(ifile)

        if metadata_length is None:
            self.logger.info("No chunk; exiting...")
            return

        self.logger.info('Start data chunk...({},{})'.format(metadata_length, body_length))

        metadata = self._read_metadata(ifile, metadata_length)

        action = getattr(metadata, 'action', None)
        if action != 'execute':
            raise RuntimeError('Expected execute action, not {}'.format(action))

        finished = getattr(metadata, 'finished', False)
        self._record_writer.is_flushed = False

        if body_length is 0:
            return

        copy_input = True
        if copy_input:
            self._capture_input(ifile, body_length)
            reader = csv.reader(self._icopy, dialect=CsvDialect)
        else:
            reader = csv.reader(ChunkedInput(ifile, body_length), dialect=CsvDialect)

        try:
            fieldnames = next(reader)
        except StopIteration:
            raise RuntimeError('CSV header malformed')

        self.logger.debug('Read records...')
        mv_fieldnames = dict([(name, name[len('__mv_'):]) for name in fieldnames if name.startswith('__mv_')])
        if len(mv_fieldnames) == 0:
            for values in reader:
                yield OrderedDict(izip(fieldnames, values))
        else:
            for values in reader:
                record = OrderedDict()
                for fieldname, value in izip(fieldnames, values):
                    if fieldname.startswith('__mv_'):
                        if len(value) > 0:
                            record[mv_fieldnames[fieldname]] = self._decode_list(value)
                    elif fieldname not in record:
                        record[fieldname] = value
                yield record

        if not self._icopy is None:
            self._icopy.close()
            os.remove(self._icopy_path)

        if finished:
            return

        self._finished = False

    # Generator function that reads one chunk at a time processing
    # results, occasionally flushing, until the input is closed or the
    # parent reports that we are finished.  Replacement for _records()
    # from base class.
    def _our_records(self, ifile):

        maxresultrows = getattr(self._metadata.searchinfo, 'maxresultrows', 50000)

        self._finished = False
        total_count = 0
        while not self._finished:

            self.logger.debug('Read one chunk...')

            count = 0
            for record in self._one_chunk_of_records(ifile):
                count += 1
                yield record

                if count % maxresultrows == 0:
                    self._gated_flush(maxresultrows)
                    self._drain_input_one_chunk(self._ifile)
                    self.logger.info('Read one input chunk')

            self._gated_flush(count % maxresultrows)
            self.logger.info('Done one chunk ({}).'.format(count))

            total_count += count

        self.logger.info('Done with all records ({})'.format(total_count))

        self.logger.debug('Read remaining chunks...sleep {}s first'.format(1))
        time.sleep(1)
        self._drain_input(ifile)

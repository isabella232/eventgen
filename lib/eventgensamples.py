# TODO Move config settings to plugins
# TODO Remove old gen method

from __future__ import division, with_statement
import os, sys
import logging
import pprint
import random
import datetime
import re
import csv
import json
import copy
from eventgenoutput import Output
from eventgentoken import Token
from timeparser import timeParser, timeDelta2secs
from eventgencounter import Counter

class Sample:
    """
    The Sample class is the primary configuration holder for Eventgen.  Contains all of our configuration
    information for any given sample, and is passed to most objects in Eventgen and a copy is maintained
    to give that object access to configuration information.  Read and configured at startup, and each
    object maintains a threadsafe copy of Sample.
    """
    # Required fields for Sample
    name = None
    app = None
    filePath = None
    
    # Options which are all valid for a sample
    disabled = None
    spoolDir = None
    spoolFile = None
    breaker = None
    sampletype = None
    mode = None
    interval = None
    delay = None
    count = None
    bundlelines = None
    earliest = None
    latest = None
    hourOfDayRate = None
    dayOfWeekRate = None
    randomizeEvents = None
    randomizeCount = None
    outputMode = None
    fileName = None
    fileMaxBytes = None
    fileBackupFiles = None
    splunkHost = None
    splunkPort = None
    splunkMethod = None
    splunkUser = None
    splunkPass = None
    index = None
    source = None
    sourcetype = None
    host = None
    hostRegex = None
    hostToken = None
    tokens = None
    projectID = None
    accessToken = None
    backfill = None
    backfillSearch = None
    backfillSearchUrl = None
    minuteOfHourRate = None
    timeMultiple = None
    debug = None
    timezone = datetime.timedelta(days=1)
    dayOfMonthRate = None
    monthOfYearRate = None
    sessionKey = None
    splunkUrl = None
    generator = None
    rater = None
    out = None
    timeField = None
    timestamp = None
    sampleDir = None
    backfillts = None
    backfilldone = None
    stopping = False
    maxIntervalsBeforeFlush = None
    maxQueueLength = None

    
    # Internal fields
    _sampleLines = None
    sampleLines = None
    _sampleDict = None
    sampleDict = None
    _lockedSettings = None
    _priority = None
    _origName = None
    _lastts = None
    _timeSinceSleep = None
    _earliestParsed = None
    _latestParsed = None
    
    def __init__(self, name):
        # Logger already setup by config, just get an instance
        logger = logging.getLogger('eventgen')
        from eventgenconfig import EventgenAdapter
        adapter = EventgenAdapter(logger, {'module': 'Sample', 'sample': name})
        globals()['logger'] = adapter
        
        self.name = name
        self.tokens = [ ]
        self._lockedSettings = [ ]

        self._currentevent = 0
        self._rpevents = None
        self.backfilldone = False
        self._timeSinceSleep = datetime.timedelta()
        
        # Import config
        from eventgenconfig import Config
        globals()['c'] = Config()
        
    def __str__(self):
        """Only used for debugging, outputs a pretty printed representation of this sample"""
        # Eliminate recursive going back to parent
        temp = dict([ (key, value) for (key, value) in self.__dict__.items() if key != '_c' ])
        return pprint.pformat(temp)
        
    def __repr__(self):
        return self.__str__()
    
    def gen(self, count, earliesttime, latesttime):
        ret = [ ]
        logger.debug("Generating sample '%s' in app '%s'" % (self.name, self.app))
        startTime = datetime.datetime.now()

        self.timestamp = None

        # Setup initial backfillts
        if self._backfillts == None and self.backfill != None and not self._backfilldone:
            try:
                self._backfillts = timeParser(self.backfill, timezone=self.timezone)
                logger.info("Setting up backfill of %s (%s)" % (self.backfill,self._backfillts))
            except Exception as ex:
                logger.error("Failed to parse backfill '%s': %s" % (self.backfill, ex))
                raise

            if self.outputMode == "splunkstream" and self.backfillSearch != None:
                if not self.backfillSearch.startswith('search'):
                    self.backfillSearch = 'search ' + self.backfillSearch
                self.backfillSearch += '| head 1 | table _time'

                logger.debug("Searching Splunk URL '%s/services/search/jobs' with search '%s' with sessionKey '%s'" % (self.backfillSearchUrl, self.backfillSearch, self.sessionKey))

                results = httplib2.Http(disable_ssl_certificate_validation=True).request(\
                            self.backfillSearchUrl + '/services/search/jobs',
                            'POST', headers={'Authorization': 'Splunk %s' % self.sessionKey}, \
                            body=urllib.urlencode({'search': self.backfillSearch,
                                                    'earliest_time': self.backfill,
                                                    'exec_mode': 'oneshot'}))[1]
                try:
                    temptime = minidom.parseString(results).getElementsByTagName('text')[0].childNodes[0].nodeValue
                    # logger.debug("Time returned from backfill search: %s" % temptime)
                    # Results returned look like: 2013-01-16T10:59:15.411-08:00
                    # But the offset in time can also be +, so make sure we strip that out first
                    if len(temptime) > 0:
                        if temptime.find('+') > 0:
                            temptime = temptime.split('+')[0]
                        temptime = '-'.join(temptime.split('-')[0:3])
                    self._backfillts = datetime.datetime.strptime(temptime, '%Y-%m-%dT%H:%M:%S.%f')
                    logger.debug("Backfill search results: '%s' value: '%s' time: '%s'" % (pprint.pformat(results), temptime, self._backfillts))
                except (ExpatError, IndexError): 
                    pass

        # Override earliest and latest during backfill until we're at current time
        if self.backfill != None and not self._backfilldone:
            if self._backfillts >= self.now(realnow=True):
                logger.info("Backfill complete")
                # exit(1)  # Added for perf test, REMOVE LATER
                self._backfilldone = True
            else:
                logger.debug("Still backfilling for sample '%s'.  Currently at %s" % (self.name, self._backfillts))
                # if not self.mode == 'replay':
                #     self._backfillts += datetime.timedelta(seconds=self.interval)

        
        logger.debugv("Opening sample '%s' in app '%s'" % (self.name, self.app) )
        sampleFH = open(self.filePath, 'rU')
        if self.sampletype == 'raw':
            # 5/27/12 CS Added caching of the sample file
            if self._sampleLines == None:
                logger.debug("Reading raw sample '%s' in app '%s'" % (self.name, self.app))
                sampleLines = sampleFH.readlines()
                self._sampleLines = sampleLines
                sampleDict = [ ]
            else:
                sampleLines = self._sampleLines
        elif self.sampletype == 'csv':
            logger.debug("Reading csv sample '%s' in app '%s'" % (self.name, self.app))
            if self._sampleLines == None:
                logger.debug("Reading csv sample '%s' in app '%s'" % (self.name, self.app))
                sampleDict = [ ]
                sampleLines = [ ]
                # Fix to load large csv files, work with python 2.5 onwards
                csv.field_size_limit(sys.maxint)
                csvReader = csv.DictReader(sampleFH)
                for line in csvReader:
                    sampleDict.append(line)
                    try:
                        tempstr = line['_raw'].decode('string_escape')
                        if self.bundlelines:
                            tempstr = tempstr.replace('\n', 'NEWLINEREPLACEDHERE!!!')
                        sampleLines.append(tempstr)
                    except ValueError:
                        logger.error("Error in sample at line '%d' in sample '%s' in app '%s' - did you quote your backslashes?" % (csvReader.line_num, self.name, self.app))
                    except AttributeError:
                        logger.error("Missing _raw at line '%d' in sample '%s' in app '%s'" % (csvReader.line_num, self.name, self.app))
                self._sampleDict = copy.deepcopy(sampleDict)
                self._sampleLines = copy.deepcopy(sampleLines)
                logger.debug('Finished creating sampleDict & sampleLines.  Len samplesLines: %d Len sampleDict: %d' % (len(sampleLines), len(sampleDict)))
            else:
                # If we're set to bundlelines, we'll modify sampleLines regularly.
                # Since lists in python are referenced rather than copied, we
                # need to make a fresh copy every time if we're bundlelines.
                # If not, just used the cached copy, we won't mess with it.
                if not self.bundlelines:
                    sampleDict = self._sampleDict
                    sampleLines = self._sampleLines
                else:
                    sampleDict = copy.deepcopy(self._sampleDict)
                    sampleLines = copy.deepcopy(self._sampleLines)


        # Check to see if this is the first time we've run, or if we're at the end of the file
        # and we're running replay.  If so, we need to parse the whole file and/or setup our counters
        if self._rpevents == None and self.mode == 'replay':
            if self.sampletype == 'csv':
                self._rpevents = sampleDict
            else:
                if self.breaker != c.breaker:
                    self._rpevents = []
                    lines = '\n'.join(sampleLines)
                    breaker = re.search(self.breaker, lines)
                    currentchar = 0
                    while breaker:
                        self._rpevents.append(lines[currentchar:breaker.start(0)])
                        lines = lines[breaker.end(0):]
                        currentchar += breaker.start(0)
                        breaker = re.search(self.breaker, lines)
                else:
                    self._rpevents = sampleLines
            self._currentevent = 0
        
        # If we are replaying then we need to set the current sampleLines to the event
        # we're currently on
        if self.mode == 'replay':
            if self.sampletype == 'csv':
                sampleDict = [ self._rpevents[self._currentevent] ]
                sampleLines = [ self._rpevents[self._currentevent]['_raw'].decode('string_escape') ]
            else:
                sampleLines = [ self._rpevents[self._currentevent] ]
            self._currentevent += 1
            # If we roll over the max number of lines, roll over the counter and start over
            if self._currentevent >= len(self._rpevents):
                logger.debug("At end of the sample file, starting replay from the top")
                self._currentevent = 0
                self._lastts = None

        # Ensure all lines have a newline
        for i in xrange(0, len(sampleLines)):
            if sampleLines[i][-1] != '\n':
                sampleLines[i] += '\n'

        # If we've set bundlelines, then we want count copies of all of the lines in the file
        # And we'll set breaker to be a weird delimiter so that we'll end up with an events 
        # array that can be rated by the hour of day and day of week rates
        # This is only for weird outside use cases like when we want to include a CSV file as the source
        # so we can't set breaker properly
        if self.bundlelines:
            logger.debug("Bundlelines set.  Creating %s copies of original sample lines and setting breaker." % (self.count-1))
            self.breaker = '\n------\n'
            origSampleLines = copy.deepcopy(sampleLines)
            origSampleDict = copy.deepcopy(sampleDict)
            sampleLines.append(self.breaker)
            for i in range(0, self.count-1):
                sampleLines.extend(origSampleLines)
                sampleLines.append(self.breaker)
            

        if len(sampleLines) > 0:
            if self.count == 0 and self.mode == 'sample':
                logger.debug("Count %s specified as default for sample '%s' in app '%s'; adjusting count to sample length %s; using default breaker" \
                                % (self.count, self.name, self.app, len(sampleLines)) )
                count = len(sampleLines)
                self.breaker = c.breaker

            try:
                breakerRE = re.compile(self.breaker)
            except:
                logger.error("Line breaker '%s' for sample '%s' in app '%s' could not be compiled; using default breaker" \
                            % (self.breaker, self.name, self.app) )
                self.breaker = c.breaker

            events = []
            # 9/7/13 CS If we're sampleType CSV and we do an events fill that's greater than the count
            # we don't have entries in sampleDict to match what index/host/source/sourcetype they are
            # so creating a new dict to track that metadata
            eventsDict = []
            event = ''

            if self.breaker == c.breaker:
                logger.debugv("Default breaker detected for sample '%s' in app '%s'; using simple event fill" \
                                % (self.name, self.app) )
                logger.debug("Filling events array for sample '%s' in app '%s'; count=%s, sampleLines=%s" \
                                % (self.name, self.app, count, len(sampleLines)) )

                # 5/8/12 CS Added randomizeEvents config to randomize items from the file
                # 5/27/12 CS Don't randomize unless we're raw
                try:
                    # 7/30/12 CS Can't remember why I wouldn't allow randomize Events for CSV so commenting
                    # this out and seeing what breaks
                    #if self.randomizeEvents and self.sampletype == 'raw':
                    if self.randomizeEvents:
                        logger.debugv("Shuffling events for sample '%s' in app '%s'" \
                                        % (self.name, self.app))
                        random.shuffle(sampleLines)
                except:
                    logger.error("randomizeEvents for sample '%s' in app '%s' unparseable." \
                                    % (self.name, self.app))
                
                if count >= len(sampleLines):
                    events = sampleLines
                    if self.sampletype == 'csv':
                        eventsDict = sampleDict[:]
                else:
                    events = sampleLines[0:count]
                    if self.sampletype == 'csv':
                        eventsDict = sampleDict[0:count]
            else:
                logger.debugv("Non-default breaker '%s' detected for sample '%s' in app '%s'; using advanced event fill" \
                                % (self.breaker, self.name, self.app) ) 

                ## Fill events array from breaker and sampleLines
                breakersFound = 0
                x = 0

                logger.debug("Filling events array for sample '%s' in app '%s'; count=%s, sampleLines=%s" \
                                % (self.name, self.app, count, len(sampleLines)) )
                while len(events) < count and x < len(sampleLines):
                    #logger.debug("Attempting to match regular expression '%s' with line '%s' for sample '%s' in app '%s'" % (breaker, sampleLines[x], sample, app) )
                    breakerMatch = breakerRE.search(sampleLines[x])

                    if breakerMatch:
                        #logger.debug("Match found for regular expression '%s' and line '%s' for sample '%s' in app '%s'" % (breaker, sampleLines[x], sample, app) )
                        ## If not first
                        # 5/28/12 CS This may cause a regression defect, but I can't figure out why
                        # you'd want to ignore the first breaker you find.  It's certainly breaking
                        # my current use case.

                        # 6/25/12 CS Definitely caused a regression defect.  I'm going to add
                        # a check for bundlelines which is where I need this to work every time
                        if breakersFound != 0 or self.bundlelines:
                            events.append(event)
                            event = ''

                        breakersFound += 1
                    # else:
                    #     logger.debug("Match not found for regular expression '%s' and line '%s' for sample '%s' in app '%s'" % (breaker, sampleLines[x], sample, app) )

                    # If we've inserted the breaker with bundlelines, don't insert the line, otherwise insert
                    if not (self.bundlelines and breakerMatch):
                        event += sampleLines[x]
                    x += 1

                ## If events < count append remaining data in samples
                if len(events) < count:
                    events.append(event + '\n')

                if self.bundlelines:
                    eventsDict = sampleDict[:]

                ## If breaker wasn't found in sample
                ## events = sample
                if breakersFound == 0:
                    logger.warn("Breaker '%s' not found for sample '%s' in app '%s'; using default breaker" % (self.breaker, self.name, self.app) )

                    if count >= len(sampleLines):
                        events = sampleLines
                    else:
                        events = sampleLines[0:count]
                else:
                    logger.debugv("Found '%s' breakers for sample '%s' in app '%s'" % (breakersFound, self.name, self.app) )

            ## Continue to fill events array until len(events) == count
            if len(events) > 0 and len(events) < count:
                logger.debugv("Events fill for sample '%s' in app '%s' less than count (%s vs. %s); continuing fill" % (self.name, self.app, len(events), count) )
                tempEvents = events[:]
                if self.sampletype == 'csv':
                    tempEventsDict = eventsDict[:]
                while len(events) < count:
                    y = 0
                    while len(events) < count and y < len(tempEvents):
                        events.append(tempEvents[y])
                        if self.sampletype == 'csv':
                            eventsDict.append(tempEventsDict[y])
                        y += 1

            # logger.debug("events: %s" % pprint.pformat(events))
            logger.debug("Replacing %s tokens in %s events for sample '%s' in app '%s'" % (len(self.tokens), len(events), self.name, self.app))

            if self.sampletype == 'csv' and len(eventsDict) > 0:
                self.index = eventsDict[0]['index']
                self.host = eventsDict[0]['host']
                self.source = eventsDict[0]['source']
                self.sourcetype = eventsDict[0]['sourcetype']
                logger.debugv("Sampletype CSV.  Setting CSV parameters. index: '%s' host: '%s' source: '%s' sourcetype: '%s'" \
                            % (self.index, self.host, self.source, self.sourcetype))
                
            # Find interval before we muck with the event but after we've done event breaking
            if self.mode == 'replay':
                logger.debugv("Finding timestamp to compute interval for events")
                if self._lastts == None:
                    if self.sampletype == 'csv':
                        self._lastts = self._getTSFromEvent(self._rpevents[self._currentevent][self.timeField])
                    else:
                        self._lastts = self._getTSFromEvent(self._rpevents[self._currentevent])
                if (self._currentevent+1) < len(self._rpevents):
                    if self.sampletype == 'csv':
                        nextts = self._getTSFromEvent(self._rpevents[self._currentevent+1][self.timeField])
                    else:
                        nextts = self._getTSFromEvent(self._rpevents[self._currentevent+1])
                else:
                    logger.debug("At end of _rpevents")
                    return 0

                logger.debugv('Computing timeDiff nextts: "%s" lastts: "%s"' % (nextts, self._lastts))

                timeDiff = nextts - self._lastts
                if timeDiff.days >= 0 and timeDiff.seconds >= 0 and timeDiff.microseconds >= 0:
                    partialInterval = float("%d.%06d" % (timeDiff.seconds, timeDiff.microseconds))
                else:
                    partialInterval = 0

                if self.timeMultiple > 0:
                    partialInterval *= self.timeMultiple

                logger.debugv("Setting partialInterval for replay mode with timeMultiple %s: %s %s" % (self.timeMultiple, timeDiff, partialInterval))
                self._lastts = nextts

            ## Iterate events
            for x in range(0, len(events)):
                event = events[x]

                # Maintain state for every token in a given event
                # Hash contains keys for each file name which is assigned a list of values
                # picked from a random line in that file
                mvhash = { }

                ## Iterate tokens
                for token in self.tokens:
                    token.mvhash = mvhash
                    event = token.replace(event)
                if(self.hostToken):
                    # clear the host mvhash every time, because we need to re-randomize it
                    self.hostToken.mvhash =  {}

                # Hack for bundle lines to work with sampletype csv
                # Basically, bundlelines allows us to create copies of a bundled set of
                # of events as one event, and this splits those back out so that we properly
                # send each line with the proper sourcetype and source if we're we're sampletype csv
                if self.bundlelines and self.sampletype == 'csv':
                    # Trim last newline so we don't end up with blank at end of the array
                    if event[-1] == '\n':
                        event = event[:-1]
                    lines = event.split('\n')
                    logger.debugv("Bundlelines set and sampletype csv, breaking event back apart.  %d lines %d eventsDict." % (len(lines), len(eventsDict)))
                    for lineno in range(0, len(lines)):
                        if self.sampletype == 'csv' and (eventsDict[lineno]['index'] != self.index or \
                                                         eventsDict[lineno]['host'] != self.host or \
                                                         eventsDict[lineno]['source'] != self.source or \
                                                         eventsDict[lineno]['sourcetype'] != self.sourcetype):
                            self.index = eventsDict[lineno]['index']
                            self.host = eventsDict[lineno]['host']
                            # Allow randomizing the host:
                            if(self.hostToken):
                                self.host = self.hostToken.replace(self.host)

                            self.source = eventsDict[lineno]['source']
                            self.sourcetype = eventsDict[lineno]['sourcetype']
                            logger.debugv("Sampletype CSV.  Setting CSV parameters. index: '%s' host: '%s' source: '%s' sourcetype: '%s'" \
                                         % (self.index, self.host, self.source, self.sourcetype))
                        self.out.send(lines[lineno].replace('NEWLINEREPLACEDHERE!!!', '\n'))
                else:
                    # logger.debug("Sample Index: %s Host: %s Source: %s Sourcetype: %s" % (self.index, self.host, self.source, self.sourcetype))
                    # logger.debug("Event Index: %s Host: %s Source: %s Sourcetype: %s" % (sampleDict[x]['index'], sampleDict[x]['host'], sampleDict[x]['source'], sampleDict[x]['sourcetype']))
                    if self.sampletype == 'csv' and (eventsDict[x]['index'] != self.index or \
                                                    eventsDict[x]['host'] != self.host or \
                                                    eventsDict[x]['source'] != self.source or \
                                                    eventsDict[x]['sourcetype'] != self.sourcetype):
                        self.index = sampleDict[x]['index']
                        self.host = sampleDict[x]['host']
                        # Allow randomizing the host:
                        if(self.hostToken):
                            self.host = self.hostToken.replace(self.host)

                        self.source = sampleDict[x]['source']
                        self.sourcetype = sampleDict[x]['sourcetype']
                        logger.debugv("Sampletype CSV.  Setting CSV parameters. index: '%s' host: '%s' source: '%s' sourcetype: '%s'" \
                                    % (self.index, self.host, self.source, self.sourcetype))
                    self.out.send(event)

            ## Close file handles
            sampleFH.close()

            endTime = datetime.datetime.now()
            timeDiff = endTime - startTime

            if self.mode == 'sample':
                # timeDiffSecs = timeDelta2secs(timeDiff)
                timeDiffSecs = float("%d.%06d" % (timeDiff.seconds, timeDiff.microseconds))
                wholeIntervals = timeDiffSecs / self.interval
                partialInterval = timeDiffSecs % self.interval

                if wholeIntervals > 1:
                    logger.warn("Generation of sample '%s' in app '%s' took longer than interval (%s seconds vs. %s seconds); consider adjusting interval" \
                                % (self.name, self.app, timeDiff, self.interval) )

                partialInterval = self.interval - partialInterval
            
            # No rest for the wicked!  Or while we're doing backfill
            if self.backfill != None and not self._backfilldone:
                # Since we would be sleeping, increment the timestamp by the amount of time we're sleeping
                incsecs = round(partialInterval / 1, 0)
                incmicrosecs = partialInterval % 1
                self._backfillts += datetime.timedelta(seconds=incsecs, microseconds=incmicrosecs)
                partialInterval = 0

            self._timeSinceSleep += timeDiff
            if partialInterval > 0:
                timeDiffFrac = "%d.%06d" % (self._timeSinceSleep.seconds, self._timeSinceSleep.microseconds)
                logger.info("Generation of sample '%s' in app '%s' completed in %s seconds.  Sleeping for %f seconds" \
                            % (self.name, self.app, timeDiffFrac, partialInterval) )
                self._timeSinceSleep = datetime.timedelta()
            return partialInterval
        else:
            logger.warn("Sample '%s' in app '%s' contains no data" % (self.name, self.app) )
        
    ## Replaces $SPLUNK_HOME w/ correct pathing
    def pathParser(self, path):
        greatgreatgrandparentdir = os.path.dirname(os.path.dirname(c.grandparentdir)) 
        sharedStorage = ['$SPLUNK_HOME/etc/apps', '$SPLUNK_HOME/etc/users/', '$SPLUNK_HOME/var/run/splunk']

        ## Replace windows os.sep w/ nix os.sep
        path = path.replace('\\', '/')
        ## Normalize path to os.sep
        path = os.path.normpath(path)

        ## Iterate special paths
        for x in range(0, len(sharedStorage)):
            sharedPath = os.path.normpath(sharedStorage[x])

            if path.startswith(sharedPath):
                path.replace('$SPLUNK_HOME', greatgreatgrandparentdir)
                break

        ## Split path
        path = path.split(os.sep)

        ## Iterate path segments
        for x in range(0, len(path)):
            segment = path[x].lstrip('$')
            ## If segement is an environment variable then replace
            if os.environ.has_key(segment):
                path[x] = os.environ[segment]

        ## Join path
        path = os.sep.join(path)

        return path

    def getTSFromEvent(self, event):
        currentTime = None
        formats = [ ]
        # JB: 2012/11/20 - Can we optimize this by only testing tokens of type = *timestamp?
        # JB: 2012/11/20 - Alternatively, documentation should suggest putting timestamp as token.0.
        for token in self.tokens:
            try:
                formats.append(token.token)
                # logger.debug("Searching for token '%s' in event '%s'" % (token.token, event))
                results = token._search(event)
                if results:
                    timeFormat = token.replacement
                    group = 0 if len(results.groups()) == 0 else 1
                    timeString = results.group(group)
                    # logger.debug("Testing '%s' as a time string against '%s'" % (timeString, timeFormat))
                    if timeFormat == "%s":
                        ts = float(timeString) if len(timeString) < 10 else float(timeString) / (10**(len(timeString)-10))
                        logger.debugv("Getting time for timestamp '%s'" % ts)
                        currentTime = datetime.datetime.fromtimestamp(ts)
                    else:
                        logger.debugv("Getting time for timeFormat '%s' and timeString '%s'" % (timeFormat, timeString))
                        # Working around Python bug with a non thread-safe strptime.  Randomly get AttributeError
                        # when calling strptime, so if we get that, try again
                        while currentTime == None:
                            try:
                                currentTime = datetime.datetime.strptime(timeString, timeFormat)
                            except AttributeError:
                                pass
                    logger.debugv("Match '%s' Format '%s' result: '%s'" % (timeString, timeFormat, currentTime))
                    if type(currentTime) == datetime.datetime:
                        break
            except ValueError:
                logger.debug("Match found ('%s') but time parse failed. Timeformat '%s' Event '%s'" % (timeString, timeFormat, event))
        if type(currentTime) != datetime.datetime:
            # Total fail
            logger.error("Can't find a timestamp (using patterns '%s') in this event: '%s'." % (formats, event))
            raise ValueError("Can't find a timestamp (using patterns '%s') in this event: '%s'." % (formats, event))
        # Check to make sure we parsed a year
        if currentTime.year == 1900:
            currentTime = currentTime.replace(year=self.now().year)
        return currentTime
    
    def saveState(self):
        """Saves state of all integer IDs of this sample to a file so when we restart we'll pick them up"""
        for token in self.tokens:
            if token.replacementType == 'integerid':
                stateFile = open(os.path.join(c.sampleDir, 'state.'+urllib.pathname2url(token.token)), 'w')
                stateFile.write(token.replacement)
                stateFile.close()

    def now(self, utcnow=False, realnow=False):
        # logger.info("Getting time (timezone %s)" % (self.timezone))
        if not self.backfilldone and not self.backfillts == None and not realnow:
            return self.backfillts
        elif self.timezone.days > 0:
            return datetime.datetime.now()
        else:
            return datetime.datetime.utcnow() + self.timezone

    def earliestTime(self):
        # First optimization, we need only store earliest and latest
        # as an offset of now if they're relative times
        if self._earliestParsed != None:
            earliestTime = self.now() - self._earliestParsed
            logger.debugv("Using cached earliest time: %s" % earliestTime)
        else:
            if self.earliest.strip()[0:1] == '+' or \
                    self.earliest.strip()[0:1] == '-' or \
                    self.earliest == 'now':
                tempearliest = timeParser(self.earliest, timezone=self.timezone)
                temptd = self.now(realnow=True) - tempearliest
                self._earliestParsed = datetime.timedelta(days=temptd.days, seconds=temptd.seconds)
                earliestTime = self.now() - self._earliestParsed
                logger.debugv("Calulating earliestParsed as '%s' with earliestTime as '%s' and self.sample.earliest as '%s'" % (self._earliestParsed, earliestTime, tempearliest))
            else:
                earliestTime = timeParser(self.earliest, timezone=self.timezone)
                logger.debugv("earliestTime as absolute time '%s'" % earliestTime)

        return earliestTime


    def latestTime(self):
        if self._latestParsed != None:
            latestTime = self.now() - self._latestParsed
            logger.debugv("Using cached latestTime: %s" % latestTime)
        else:
            if self.latest.strip()[0:1] == '+' or \
                    self.latest.strip()[0:1] == '-' or \
                    self.latest == 'now':
                templatest = timeParser(self.latest, timezone=self.timezone)
                temptd = self.now(realnow=True) - templatest
                self._latestParsed = datetime.timedelta(days=temptd.days, seconds=temptd.seconds)
                latestTime = self.now() - self._latestParsed
                logger.debugv("Calulating latestParsed as '%s' with latestTime as '%s' and self.sample.latest as '%s'" % (self._latestParsed, latestTime, templatest))
            else:
                latestTime = timeParser(self.latest, timezone=self.timezone)
                logger.debugv("latstTime as absolute time '%s'" % latestTime)

        return latestTime

    def utcnow(self):
        return self.now(utcnow=True)
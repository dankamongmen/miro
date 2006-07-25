"""httpclient.py 

Implements an HTTP client.  The main way that this module is used is the
grabURL function that that's an asynchronous version of our old grabURL.

A lot of the code here comes from inspection of the httplib standard module.
Some of it was taken more-or-less directly from there.  I (Ben Dean-Kawamura)
believe our clients follow the HTTP 1.1 spec completely, I used RFC2616 as a
reference (http://www.w3.org/Protocols/rfc2616/rfc2616.html).
"""

import errno
import re
import socket
import threading
from urlparse import urlparse, urljoin
from collections import deque
from gettext import gettext as _

from BitTornado.clock import clock

import httpauth
import config
import prefs
import dialogs
from download_utils import cleanFilename, parseURL, defaultPort
from xhtmltools import URLEncodeDict, multipartEncode
import eventloop
import util
import sys
import time

PIPELINING_ENABLED = True
SOCKET_READ_TIMEOUT = 30

# This pattern matches all possible strings.  I promise.
URIPattern = re.compile(r'^([^?]*/)?([^/?]*)/*(\?(.*))?$')

class NotReadyToSendError(Exception):
    pass
class ConnectionError(Exception):
    pass
class HTTPError(Exception):
    def __init__(self, description=None):
        self.description = description
    def __str__(self):
        if self.description is not None:
            return "%s: %s" % (self.__class__, self.description)
        else:
            return str(self.__class__)
    def getFriendlyDescription(self):
        return "HTTP Error"
class BadStatusLine(HTTPError):
    pass
class BadHeaderLine(HTTPError):
    pass
class UnexpectedStatusCode(HTTPError):
    pass
class ServerClosedConnection(HTTPError):
    def __init__(self, host):
        self.host = self.description = host
    def getFriendlyDescription(self):
        return _('%s closed connection') % self.host
class ConnectionTimeout(HTTPError):
    def __init__(self, host):
        self.host = self.description = host
    def getFriendlyDescription(self):
        return _('%s timed out') % self.host
class BadChunkSize(HTTPError):
    pass
class CRLFExpected(HTTPError):
    pass
class PipelinedRequestNeverStarted(HTTPError):
    pass
class BadRedirect(HTTPError):
    pass
class AuthorizationFailed(HTTPError):
    pass
class RequestCanceledError(HTTPError):
    pass

def trapCall(object, function, *args, **kwargs):
    """Convenience function do a util.trapCall, where when = 'While talking to
    the network'
    """
    return util.timeTrapCall("Calling %s on %s" % (function, object), function, *args, **kwargs)

class NetworkBuffer(object):
    """Responsible for storing incomming network data and doing some basic
    parsing of it.  I think this is about as fast as we can do things in pure
    python, someday we may want to make it C...
    """
    def __init__(self):
        self.chunks = []
        self.length = 0

    def addData(self, data):
        self.chunks.append(data)
        self.length += len(data)

    def _mergeChunks(self):
        self.chunks = [''.join(self.chunks)]

    def read(self, size=None):
        """Read at most size bytes from the data that has been added to the
        buffer.  """

        self._mergeChunks()
        if size is not None:
            rv = self.chunks[0][:size]
            self.chunks[0] = self.chunks[0][len(rv):]
        else:
            rv = self.chunks[0]
            self.chunks = []
        self.length -= len(rv)
        return rv

    def readline(self):
        """Like a file readline, with several difference:  
        * If there isn't a full line ready to be read we return None.  
        * Doesn't include the trailing line separator.
        * Both "\r\n" and "\n" act as a line ender
        """

        self._mergeChunks()
        split = self.chunks[0].split("\n", 1)
        if len(split) == 2:
            self.chunks[0] = split[1]
            self.length = len(self.chunks[0])
            if split[0].endswith("\r"):
                return split[0][:-1]
            else:
                return split[0]
        else:
            return None

    def unread(self, data):
        """Put back read data.  This make is like the data was never read at
        all.
        """
        self.chunks.insert(0, data)
        self.length += len(data)

    def getValue(self):
        self._mergeChunks()
        return self.chunks[0]

class _Packet(object):
    """A packet of data for the AsyncSocket class
    """
    def __init__ (self, data, callback = None):
        self.data = data
        self.callback = callback

class AsyncSocket(object):
    """Socket class that uses our new fangled asynchronous eventloop
    module.
    """

    def __init__(self, closeCallback=None):
        """Create an AsyncSocket.  If closeCallback is given, it will be
        called if we detect that the socket has been closed durring a
        read/write operation.  The arguments will be the AsyncSocket object
        and either socket.SHUT_RD or socket.SHUT_WR.
        """
        self.toSend = []
        self.readSize = 4096
        self.socket = None
        self.readCallback = None
        self.closeCallback = closeCallback
        self.readTimeout = None
        self.timedOut = False
        self.connectionErrback = None
        self.disableReadTimeout = False
        self.name = ""

    def __str__(self):
        if self.name:
            return "%s: %s" % (type(self).__name__, self.name)
        else:
            return "Unknown %s" % (type(self).__name__,)

    def startReadTimeout(self):
        if self.disableReadTimeout:
            return
        if self.readTimeout is not None:
            self.stopReadTimeout()
        self.readTimeout = eventloop.addTimeout(SOCKET_READ_TIMEOUT,
                self.onReadTimeout, "AsyncSocket.onReadTimeout")

    def stopReadTimeout(self):
        if self.readTimeout is not None:
            self.readTimeout.cancel()
            self.readTimeout = None

    def openConnection(self, host, port, callback, errback):
        """Open a connection.  On success, callback will be called with this
        object.
        """

        self.name = "Outgoing %s:%s" % (host, port)

        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setblocking(0)
        self.connectionErrback = errback
        def onAddressLookup(address):
            if self.socket is None:
                # the connection was closed while we were calling gethostbyname
                return
            try:
                self.socket.connect_ex((address, port))
            except Exception, e:
                trapCall(self, errback, e)
            else:
                eventloop.addWriteCallback(self.socket, onWriteReady)
        def onWriteReady():
            eventloop.removeWriteCallback(self.socket)
            rv = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if rv == 0:
                trapCall(self, callback, self)
            else:
                msg = errno.errorcode[rv]
                trapCall(self, errback, ConnectionError((rv, msg)))
            self.connectionErrback = None

        eventloop.callInThread(onAddressLookup, errback,
                socket.gethostbyname, host)

    def acceptConnection(self, host, port, callback, errback):
        def finishAccept():
            eventloop.removeReadCallback(self.socket)
            (self.socket, addr) = self.socket.accept()
            trapCall(self, callback, self)
            self.connectionErrback = None

        self.name = "Incoming %s:%s" % (host, port)
        self.connectionErrback = errback
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind( (host, port) )
        (self.addr, self.port) = self.socket.getsockname()
        self.socket.listen(63)
        eventloop.addReadCallback(self.socket, finishAccept)

    def closeConnection(self):
        if self.isOpen():
            eventloop.stopHandlingSocket(self.socket)
            self.stopReadTimeout()
            self.socket.close()
            self.socket = None
            if self.connectionErrback is not None:
                error = ConnectionError("Connection closed")
                trapCall(self, self.connectionErrback, error)
                self.connectionErrback = None

    def isOpen(self):
        return self.socket is not None

    def sendData(self, data, callback = None):
        """Send data out to the socket when it becomes ready.
        
        NOTE: currently we have no way of detecting when the data gets sent
        out, or if errors happen.
        """

        if not self.isOpen():
            raise ValueError("Socket not connected")
        self.toSend.append(_Packet(data, callback))
        eventloop.addWriteCallback(self.socket, self.onWriteReady)

    def startReading(self, readCallback):
        """Start reading from the socket.  When data becomes available it will
        be passed to readCallback.  If there is already a read callback, it
        will be replaced.
        """

        if not self.isOpen():
            raise ValueError("Socket not connected")
        self.readCallback = readCallback
        eventloop.addReadCallback(self.socket, self.onReadReady)
        self.startReadTimeout()

    def stopReading(self):
        """Stop reading from the socket."""

        if not self.isOpen():
            raise ValueError("Socket not connected")
        self.readCallback = None
        eventloop.removeReadCallback(self.socket)
        self.stopReadTimeout()

    def onReadTimeout(self):
        self.stopReadTimeout()
        self.timedOut = True
        self.handleEarlyClose('read')

    def handleSocketError(self, code, msg, operation):
        if code in (errno.EWOULDBLOCK, errno.EINTR):
            return

        if operation == "write":
            expectedErrors = (errno.EPIPE, errno.ECONNRESET)
        else:
            expectedErrors = (errno.ECONNREFUSED, errno.ECONNRESET)
        if code not in expectedErrors:
            print "WARNING, got unexpected error during %s" % operation
            print "%s: %s" % (errno.errorcode.get(code), msg)
        self.handleEarlyClose(operation)

    def onWriteReady(self):
        try:
            if len(self.toSend) > 0:
                sent = self.socket.send(self.toSend[0].data)
            else:
                sent = 0
        except socket.error, (code, msg):
            self.handleSocketError(code, msg, "write")
        else:
            self.handleSentData(sent)

    def handleSentData(self, sent):
        if len(self.toSend) > 0:
            self.toSend[0].data = self.toSend[0].data[sent:]
            if len(self.toSend[0].data) == 0:
                if self.toSend[0].callback:
                    self.toSend[0].callback()
                self.toSend = self.toSend[1:]
        if len(self.toSend) == 0:
            eventloop.removeWriteCallback(self.socket)

    def onReadReady(self):
        try:
            data = self.socket.recv(self.readSize)
        except socket.error, (code, msg):
            self.handleSocketError(code, msg, "read")
        else:
            self.handleReadData(data)

    def handleReadData(self, data):
        self.startReadTimeout()
        if data == '':
            if self.closeCallback:
                trapCall(self, self.closeCallback, self, socket.SHUT_RD)
        else:
            trapCall(self, self.readCallback, data)

    def handleEarlyClose(self, operation):
        self.closeConnection()
        if self.closeCallback:
            if operation == 'read':
                type = socket.SHUT_RD
            else:
                type = socket.SHUT_WR
            trapCall(self, self.closeCallback, self, type)

class AsyncSSLStream(AsyncSocket):
    def __init__(self, closeCallback=None):
        super(AsyncSSLStream, self).__init__(closeCallback)
        self.interruptedOperation = None

    def openConnection(self, host, port, callback, errback):
        def onSocketOpen(self):
            self.socket.setblocking(1)
            eventloop.callInThread(onSSLOpen, errback, socket.ssl,
                    self.socket)
        def onSSLOpen(ssl):
            if self.socket is None:
                # the connection was closed while we were calling socket.ssl
                return
            self.socket.setblocking(0)
            self.ssl = ssl
            # finally we can call the actuall callback
            callback(self)
        super(AsyncSSLStream, self).openConnection(host, port, onSocketOpen,
                errback)

    def resumeNormalCallbacks(self):
        if self.readCallback is not None:
            eventloop.addReadCallback(self.socket, self.onReadReady)
        if len(self.toSend) != 0:
            eventloop.addWriteCallback(self.socket, self.onWriteReady)

    def handleSocketError(self, code, msg, operation):
        if code in (socket.SSL_ERROR_WANT_READ, socket.SSL_ERROR_WANT_WRITE):
            if self.interruptedOperation is None:
                self.interruptedOperation = operation
            elif self.interruptedOperation != operation:
                util.failed("When talking to the network", 
                details="socket error for the wrong SSL operation")
                self.closeConnection()
                return
            eventloop.stopHandlingSocket(self.socket)
            if code == socket.SSL_ERROR_WANT_READ:
                eventloop.addReadCallback(self.socket, self.onReadReady)
            else:
                eventloop.addWriteCallback(self.socket, self.onWriteReady)
        elif code in (socket.SSL_ERROR_ZERO_RETURN, socket.SSL_ERROR_SSL,
                socket.SSL_ERROR_SYSCALL, socket.SSL_ERROR_EOF):
            self.handleEarlyClose(operation)
        else:
            super(AsyncSSLStream, self).handleSocketError(code, msg,
                    operation)

    def onWriteReady(self):
        if self.interruptedOperation == 'read':
            return self.onReadReady()
        try:
            if len(self.toSend) > 0:
                sent = self.ssl.write(self.toSend[0].data)
            else:
                sent = 0
        except socket.error, (code, msg):
            self.handleSocketError(code, msg, "write")
        else:
            if self.interruptedOperation == 'write':
                self.resumeNormalCallbacks()
                self.interruptedOperation = None
            self.handleSentData(sent)

    def onReadReady(self):
        if self.interruptedOperation == 'write':
            return self.onWriteReady()
        try:
            data = self.ssl.read(self.readSize)
        except socket.error, (code, msg):
            self.handleSocketError(code, msg, "read")
        else:
            if self.interruptedOperation == 'read':
                self.resumeNormalCallbacks()
                self.interruptedOperation = None
            self.handleReadData(data)

class ConnectionHandler(object):
    """Base class to handle asynchronous network streams.  It implements a
    simple state machine to deal with incomming data.

    Sending data: Use the sendData() method.

    Reading Data: Add entries to the state dictionary, which maps strings to
    methods.  The state methods will be called when there is data available,
    which can be read from the buffer variable.  The states dictionary can
    contain a None value, to signal that the handler isn't interested in
    reading at that point.  Use changeState() to switch states.

    Subclasses should override tho the handleClose() method to handle the
    socket closing.
    """

    streamFactory = AsyncSocket

    def __init__(self):
        self.buffer = NetworkBuffer()
        self.states = {'initializing': None, 'closed': None}
        self.stream = self.streamFactory(closeCallback=self.closeCallback)
        self.changeState('initializing')
        self.name = ""

    def __str__(self):
        if self.name:
            return "%s: %s" % (type(self).__name__, self.name)
        else:
            return "Unknown %s" % (type(self).__name__,)

    def openConnection(self, host, port, callback, errback):
        self.name = "Outgoing %s:%s" % (host, port)
        self.host = host
        self.port = port
        def callbackIntercept(asyncSocket):
            if callback:
                trapCall(self, callback, self)
        self.stream.openConnection(host, port, callbackIntercept, errback)

    def closeConnection(self):
        if self.stream.isOpen():
            self.stream.closeConnection()
        self.changeState('closed')

    def sendData(self, data, callback = None):
        self.stream.sendData(data, callback)

    def changeState(self, newState):
        self.readHandler = self.states[newState]
        self.state = newState
        self.updateReadCallback()

    def updateReadCallback(self):
        if self.readHandler is not None:
            self.stream.startReading(self.handleData)
        elif self.stream.isOpen():
            try:
                self.stream.stopReading()
            except KeyError:
                pass

    def handleData(self, data):
        self.buffer.addData(data)
        lastState = self.state
        self.readHandler()
        # If we switch states, continue processing the buffer.  There may be
        # extra data that the last read handler didn't read in
        while self.readHandler is not None and lastState != self.state:
            lastState = self.state
            self.readHandler()

    def closeCallback(self, stream, type):
        self.handleClose(type)

    def handleClose(self, type):
        """Handle our stream becoming closed.  Type is either socket.SHUT_RD,
        or socket.SHUT_WR.
        """
        raise NotImplementedError()

    def __str__(self):
        return "%s -- %s" % (self.__class__, self.state)

class HTTPConnection(ConnectionHandler):
    scheme = 'http'

    def __init__(self, closeCallback=None, readyCallback=None):
        super(HTTPConnection, self).__init__()
        self.shortVersion = 0
        self.states['ready'] = None
        self.states['response-status'] = self.onStatusData
        self.states['response-headers'] = self.onHeaderData
        self.states['response-body'] = self.onBodyData
        self.states['chunk-size'] = self.onChunkSizeData
        self.states['chunk-data'] = self.onChunkData
        self.states['chunk-crlf'] = self.onChunkCRLFData
        self.states['chunk-trailer'] = self.onChunkTrailerData
        self.changeState('ready')
        self.idleSince = clock()
        self.unparsedHeaderLine = ''
        self.pipelinedRequest = None
        self.closeCallback = closeCallback
        self.readyCallback = readyCallback
        self.requestsFinished = 0
        self.bytesRead = 0
        self.sentReadyCallback = False
        self.headerCallback = self.bodyDataCallback = None

    def handleData(self, data):
        self.bytesRead += len(data)
        super(HTTPConnection, self).handleData(data)

    def closeConnection(self):
        super(HTTPConnection, self).closeConnection()
        if self.closeCallback is not None:
            self.closeCallback(self)
            self.closeCallback = None
        self.checkPipelineNotStarted()

    def checkPipelineNotStarted(self):
        """Call this when the connection is closed by Democracy or the other
        side.  It will check if we have an unstarted pipeline request and 
        send it the PipelinedRequestNeverStarted error
        """

        if self.pipelinedRequest is not None:
            errback = self.pipelinedRequest[1]
            trapCall(self, errback, PipelinedRequestNeverStarted())

    def canSendRequest(self):
        return (self.state == 'ready' or 
                (self.state != 'closed' and self.pipelinedRequest is None and
                    not self.willClose and PIPELINING_ENABLED))

    def sendRequest(self, callback, errback, requestStartCallback=None,
            headerCallback=None, bodyDataCallback = None, method="GET",
            path='/', headers=None, postVariables = None, postFiles = None):
        """Sending an HTTP Request.  callback will be called if the request
        completes normally, errback will be called if there is a network
        error.

        Callback will be passed a dictionary that represents the HTTP
        response,  it will have an entry for each header sent by the server as
        well as as the following keys:
            body, version, status, reason, method, path, host, port
        They should be self explanatory, status and port will be integers, the
        other items will be strings.

        If requestStartCallback is given, it will be called just before the
        we start receiving data for the request (this can be a while after
        sending the request in the case of pipelined requests).  It will be
        passed this connection object.

        If headerCallback is given, it will be called when the headers are
        read in.  It will be passed a response object whose body is set to
        None.

        If bodyDataCallback is given it will be called as we read in the data
        for the body.  Also, the connection won't store the body in memory,
        and the callback is called, it will be passed None for the body.

        postVariables is a dictionary of variable names to values

        postFiles is a dictionary of variable names to dictionaries
        containing filename, mimetype, and handle attributes. Handle
        should be an already open file handle.
        """

        if not self.canSendRequest():
            raise NotReadyToSendError()

        if headers is None:
            headers = {}
        else:
            headers = headers.copy()
        headers['Host'] = self.host.encode('idna')
        if self.port != defaultPort(self.scheme):
            headers['Host'] += ':%d' % self.port
        headers['Accept-Encoding'] = 'identity'

        if (method == "POST" and postVariables is not None and
                            len(postVariables) > 0 and postFiles is None):
            postData = URLEncodeDict(postVariables)
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            headers['Content-Length'] = '%d' % len(postData)
        elif method == "POST" and postFiles is not None:
            (postData, boundary) = multipartEncode(postVariables, postFiles)
            headers['Content-Type'] = 'multipart/form-data; boundary=%s' % boundary
            headers['Content-Length'] = '%d' % len(postData)            
        else:
            postData = None

        self.sendRequestData(method, path, headers, postData)
        args = (callback, errback, requestStartCallback, headerCallback,
                bodyDataCallback, method, path, headers)
        if self.state == 'ready':
            self.startNewRequest(*args)
        else:
            self.pipelinedRequest = args

    def startNewRequest(self, callback, errback, requestStartCallback,
            headerCallback, bodyDataCallback, method, path, headers):
        """Called when we're ready to start processing a new request, either
        because one has just been made, or because we've pipelined one, and
        the previous request is done.
        """

        if requestStartCallback:
            trapCall(self, requestStartCallback, self)
            if self.state == 'closed':
                return

        self.callback = callback
        self.errback = errback
        self.headerCallback = headerCallback
        self.bodyDataCallback = bodyDataCallback
        self.method = method
        self.path = path
        self.requestHeaders = headers
        self.headers = {}
        self.contentLength = self.version = self.status = self.reason = None
        self.bytesRead = 0
        self.body = ''
        self.willClose = True 
        # Assume we will close, until we get the headers
        self.chunked = False
        self.chunks = []
        self.idleSince = None
        self.sentReadyCallback = False
        self.changeState('response-status')

    def sendRequestData(self, method, path, headers, data = None):
        sendOut = []
        sendOut.append('%s %s HTTP/1.1\r\n' % (method, path))
        for header, value in headers.items():
            sendOut.append('%s: %s\r\n' % (header, value))
        sendOut.append('\r\n')
        if data is not None:
            sendOut.append(data)
        self.sendData(''.join(sendOut))

    def onStatusData(self):
        line = self.buffer.readline()
        if line is not None:
            self.handleStatusLine(line)
            if self.state == 'closed':
                return
            if self.shortVersion != 9:
                self.changeState('response-headers')
            else:
                self.startBody()

    def onHeaderData(self):
        while self.state == 'response-headers':
            line = self.buffer.readline()
            if line is None:
                break
            self.handleHeaderLine(line)
        
    def onBodyData(self):
        if self.bodyDataCallback:
            if self.contentLength is None:
                data = self.buffer.read()
            else:
                bytesLeft = self.contentLength - self.bodyBytesRead
                data = self.buffer.read(bytesLeft)
            if data == '':
                return
            self.bodyBytesRead += len(data)
            trapCall(self, self.bodyDataCallback, data)
            if self.state == 'closed':
                return 
            if (self.contentLength is not None and 
                    self.bodyBytesRead == self.contentLength):
                self.finishRequest()
        elif (self.contentLength is not None and 
                self.buffer.length >= self.contentLength):
            self.body = self.buffer.read(self.contentLength)
            self.finishRequest()

    def onChunkSizeData(self):
        line = self.buffer.readline()
        if line is not None:
            sizeString = line.split(';', 1)[0] # ignore chunk-extensions
            try:
                self.chunkSize = int(sizeString, 16)
            except ValueError:
                self.handleError(BadChunkSize(line))
                return
            if self.chunkSize != 0:
                self.chunkBytesRead = 0
                self.changeState('chunk-data')
            else:
                self.changeState('chunk-trailer')

    def onChunkData(self):
        if self.bodyDataCallback:
            bytesLeft = self.chunkSize - self.chunkBytesRead
            data = self.buffer.read(bytesLeft)
            self.chunkBytesRead += len(data)
            if data == '':
                return
            trapCall(self, self.bodyDataCallback, data)
            if self.chunkBytesRead == self.chunkSize:
                self.changeState('chunk-crlf')
        elif self.buffer.length >= self.chunkSize:
            self.chunks.append(self.buffer.read(self.chunkSize))
            self.changeState('chunk-crlf')

    def onChunkCRLFData(self):
        if self.buffer.length >= 2:
            crlf = self.buffer.read(2)
            if crlf != "\r\n":
                self.handleError(CRLFExpected(crlf))
            else:
                self.changeState('chunk-size')

    def onChunkTrailerData(self):
        # discard all trailers, we shouldn't have any
        line = self.buffer.readline()
        while line is not None:
            if line == '':
                self.finishRequest()
                break
            line = self.buffer.readline()

    def handleStatusLine(self, line):
        try:
            (version, status, reason) = line.split(None, 2)
        except ValueError:
            try:
                (version, status) = line.split(None, 1)
                reason = ""
            except ValueError:
                # empty version will cause next test to fail and status
                # will be treated as 0.9 response.
                version = ""
        if not version.startswith('HTTP/'):
            # assume it's a Simple-Response from an 0.9 server
            self.buffer.unread(line + '\r\n')
            self.version = "HTTP/0.9"
            self.status = 200
            self.reason = ""
            self.shortVersion = 9
        else:
            try:
                status = int(status)
                if status < 100 or status > 599:
                    self.handleError(BadStatusLine(line))
                    return
            except ValueError:
                self.handleError(BadStatusLine(line))
                return
            if version == 'HTTP/1.0':
                self.shortVersion = 10
            elif version.startswith('HTTP/1.'):
                # use HTTP/1.1 code for HTTP/1.x where x>=1
                self.shortVersion = 11
            else:
                self.handleError(BadStatusLine(line))
                return
            self.version = version
            self.status = status
            self.reason = reason

    def handleHeaderLine(self, line):
        if self.unparsedHeaderLine == '':
            if line == '':
                if self.status != 100:
                    self.startBody()
                else:
                    self.changeState('response-status')
            elif ':' in line:
                self.parseHeader(line)
            else:
                self.unparsedHeaderLine = line
        else:
            # our last line may have been a continued header, or it may be
            # garbage, 
            if len(line) > 0 and line[0] in (' ', '\t'):
                self.unparsedHeaderLine += line.lstrip()
                if ':' in self.unparsedHeaderLine:
                    self.parseHeader(self.unparsedHeaderLine)
                    self.unparsedHeaderLine = ''
            else:
                msg = "line: %s, next line: %s" % (self.unparsedHeaderLine, 
                        line)
                self.handleError(BadHeaderLine(msg))

    def parseHeader(self, line):
        header, value = line.split(":", 1)
        value = value.strip()
        header = header.lstrip().lower()
        if value == '':
            print "DTV: Warning: Bad Header from %s:%s%s (%s)" % (self.host, self.port, self.path, line)
        if header not in self.headers:
            self.headers[header] = value
        else:
            self.headers[header] += (',%s' % value)

    def startBody(self):
        self.findExpectedLength()
        self.checkChunked()
        self.decideWillClose()
        if self.headerCallback:
            trapCall(self, self.headerCallback, self.makeResponse())
        if self.state == 'closed':
            return # maybe the header callback cancelled this request
        if ((100 <= self.status <= 199) or self.status in (204, 304) or
                self.method == 'HEAD' or self.contentLength == 0):
            self.finishRequest()
        else:
            if self.bodyDataCallback:
                self.bodyBytesRead = 0
            if not self.chunked:
                self.changeState('response-body')
            else:
                self.changeState('chunk-size')
        self.maybeSendReadyCallback()

    def checkChunked(self):
        te = self.headers.get('transfer-encoding', '')
        self.chunked = (te.lower() == 'chunked')

    def findExpectedLength(self):
        self.contentLength = None
        if self.status == 416:
            try:
                contentRange = self.headers['content-range']
            except KeyError:
                pass
            else:
                m = re.search('bytes\s+\*/(\d+)', contentRange)
                if m is not None:
                    try:
                        self.contentLength = int(m.group(1))
                    except (ValueError, TypeError):
                        pass
        if (self.contentLength is None and 
                self.headers.get('transfer-encoding') in ('identity', None)):
            try:
                self.contentLength = int(self.headers['content-length'])
            except (ValueError, KeyError):
                pass
        if self.contentLength < 0:
            self.contentLength = None

    def decideWillClose(self):
        if self.shortVersion != 11:
            # Close all connections to HTTP/1.0 servers.
            self.willClose = True
        elif 'close' in self.headers.get('connection', '').lower():
            self.willClose = True
        elif not self.chunked and self.contentLength is None:
            # if we aren't chunked and didn't get a content length, we have to
            # assume the connection will close
            self.willClose = True
        else:
            # HTTP/1.1 connections are assumed to stay open 
            self.willClose = False

    def finishRequest(self):
        # calculate the response and and remember our callback.  They may
        # change after we start a pielined response.
        origCallback = self.callback 
        if self.bodyDataCallback:
            body = None
        elif self.chunked:
            body = ''.join(self.chunks)
        else:
            body = self.body
        response = self.makeResponse(body)
        if self.stream.isOpen():
            if self.willClose:
                self.closeConnection()
                self.changeState('closed')
            elif self.pipelinedRequest is not None:
                req = self.pipelinedRequest
                self.pipelinedRequest = None
                self.startNewRequest(*req)
            else:
                self.changeState('ready')
                self.idleSince = clock()
        trapCall(self, origCallback, response)
        self.requestsFinished += 1
        self.maybeSendReadyCallback()

    def makeResponse(self, body=None):
        response = self.headers.copy()
        response['body'] = body
        for key in ('version', 'status', 'reason', 'method', 'path', 'host',
                'port', 'contentLength'):
            response[key] = getattr(self, key)
        return response

    def maybeSendReadyCallback(self):
        if (self.readyCallback and self.canSendRequest() and not
                self.sentReadyCallback):
            self.sentReadyCallback = True
            self.readyCallback(self)
        
    def handleClose(self, type):
        oldState = self.state
        self.closeConnection()
        if oldState == 'response-body' and self.contentLength is None:
            self.body = self.buffer.read()
            self.finishRequest()
        elif self.stream.timedOut:
            self.errback(ConnectionTimeout(self.host))
        else:
            self.errback(ServerClosedConnection(self.host))
        self.checkPipelineNotStarted()

    def handleError(self, error):
        self.closeConnection()
        trapCall(self, self.errback, error)

class HTTPSConnection(HTTPConnection):
    streamFactory = AsyncSSLStream
    scheme = 'https'

class HTTPConnectionPool(object):
    """Handle a pool of HTTP connections.

    We use the following stategy to handle new requests:
    * If there is an connection on the server that's ready to send, use that.
    * If we haven't hit our connection limits, create a new request
    * When a connection becomes closed, we look for our last 

    NOTE: "server" in this class means the combination of the scheme, hostname
    and port.
    """

    MAX_CONNECTIONS_PER_SERVER = 2 
    CONNECTION_TIMEOUT = 300
    MAX_CONNECTIONS = 30

    def __init__(self):
        self.pendingRequests = []
        self.activeConnectionCount = 0
        self.freeConnectionCount = 0
        self.connections = {}
        eventloop.addTimeout(60, self.cleanupPool, 
            "Check HTTP Connection Timeouts")

    def _getServerConnections(self, scheme, host, port):
        key = '%s:%s:%s' % (scheme, host, port)
        try:
            return self.connections[key]
        except KeyError:
            self.connections[key] = {'free': set(), 'active': set()}
            return self.connections[key]

    def _popPendingRequest(self):
        """Try to choose a pending request to process.  If one is found,
        remove it from the pendingRequests list and return it.  If not, return
        None.
        """

        if self.activeConnectionCount >= self.MAX_CONNECTIONS:
            return None
        for i in xrange(len(self.pendingRequests)):
            req = self.pendingRequests[i]
            conns = self._getServerConnections(req['scheme'], req['host'], 
                    req['port'])
            if (len(conns['free']) > 0 or 
                    len(conns['active']) < self.MAX_CONNECTIONS_PER_SERVER):
                # This doesn't mess up the xrange above since we return immediately.
                del self.pendingRequests[i]
                return req
        return None

    def _onConnectionClosed(self, conn):
        conns = self._getServerConnections(conn.scheme, conn.host, conn.port)
        if conn in conns['active']:
            conns['active'].remove(conn)
            self.activeConnectionCount -= 1
        elif conn in conns['free']:
            conns['free'].remove(conn)
            self.freeConnectionCount -= 1
        self.runPendingRequests()

    def _onConnectionReady(self, conn):
        conns = self._getServerConnections(conn.scheme, conn.host, conn.port)
        conns['active'].remove(conn)
        self.activeConnectionCount -= 1
        conns['free'].add(conn)
        self.freeConnectionCount += 1
        self.runPendingRequests()

    def addRequest(self, callback, errback, requestStartCallback,
            headerCallback, bodyDataCallback, url, method, headers,
            postVariables = None, postFiles = None):
        """Add a request to be run.  The request will run immediately if we
        have a free connection, otherwise it will be queued.

        returns a request id that can be passed to cancelRequest
        """

        scheme, host, port, path = parseURL(url)
        if scheme not in ['http', 'https'] or host == '' or path == '':
            errback (ValueError("Bad URL: %s" % (url,)))
            return
        req = {
            'callback' : callback,
            'errback': errback,
            'requestStartCallback': requestStartCallback,
            'headerCallback': headerCallback,
            'bodyDataCallback': bodyDataCallback,
            'scheme': scheme,
            'host': host,
            'port': port,
            'method': method,
            'path': path,
            'headers': headers,
            'postVariables': postVariables,
            'postFiles': postFiles,
        }
        self.pendingRequests.append(req)
        self.runPendingRequests()

    def runPendingRequests(self):
        """Find pending requests have a free connection, otherwise it will be
        queued.
        """

        while True:
            req = self._popPendingRequest()
            if req is None:
                return
            conns = self._getServerConnections(req['scheme'], req['host'], 
                    req['port'])
            if len(conns['free']) > 0:
                conn = conns['free'].pop()
                self.freeConnectionCount -= 1
                conn.sendRequest(req['callback'], req['errback'],
                        req['requestStartCallback'], req['headerCallback'],
                        req['bodyDataCallback'], req['method'], req['path'],
                        req['headers'], req['postVariables'], req['postFiles'])
            else:
                conn = self._makeNewConnection(req)
            conns['active'].add(conn)
            self.activeConnectionCount += 1
            connectionCount = (self.activeConnectionCount +
                               self.freeConnectionCount)
            if connectionCount > self.MAX_CONNECTIONS:
                self._dropAFreeConnection()

    def _makeNewConnection(self, req):
        def openConnectionCallback(conn):
            conn.sendRequest(req['callback'], req['errback'],
                    req['requestStartCallback'], req['headerCallback'],
                    req['bodyDataCallback'], req['method'], req['path'],
                    req['headers'], req['postVariables'], req['postFiles'])
        def openConnectionErrback(error):
            conns = self._getServerConnections(req['scheme'], req['host'], 
                    req['port'])
            if conn in conns['active']:
                conns['active'].remove(conn)
                self.activeConnectionCount -= 1
            req['errback'](error)

        if req['scheme'] == 'http':
            conn = HTTPConnection(self._onConnectionClosed,
                    self._onConnectionReady) 
        elif req['scheme'] == 'https':
            conn = HTTPSConnection(self._onConnectionClosed,
                    self._onConnectionReady) 
        else:
            raise AssertionError ("Code shouldn't reach here.")
        conn.openConnection(req['host'], req['port'],
                openConnectionCallback, openConnectionErrback)
        return conn

    def _dropAFreeConnection(self):
        # TODO: pick based on LRU
        firstTime = sys.maxint
        toDrop = None

        for conns in self.connections.values():
            for candidate in conns['free']:
                if candidate.idleSince < firstTime:
                    toDrop = candidate
        if toDrop is not None:
            toDrop.closeConnection()

    def cleanupPool(self):
        for serverKey in self.connections.keys():
            conns = self.connections[serverKey]
            toRemove = []
            for conn in conns['free']:
                if (conn.idleSince is not None and 
                        conn.idleSince + self.CONNECTION_TIMEOUT <= clock()):
                    toRemove.append(conn)
            for x in toRemove:
                conn.closeConnection()
            if len(conns['free']) == len(conns['active']) == 0:
                del self.connections[serverKey]
        eventloop.addTimeout(60, self.cleanupPool, 
            "HTTP Connection Pool Cleanup")

class HTTPClient(object):
    """High-level HTTP client object.  
    
    HTTPClients handle a single HTTP request, but may use several
    HTTPConnections if the server returns back with a redirection status code,
    asks for authorization, etc.  Connections are pooled using an
    HTTPConnectionPool object.
    """

    connectionPool = HTTPConnectionPool() # class-wid connection pool
    MAX_REDIRECTS = 10
    MAX_AUTH_ATTEMPS = 5

    def __init__(self, url, callback, errback, headerCallback=None,
            bodyDataCallback=None, method="GET", start=0, etag=None,
            modified=None, cookies={}, postVariables = None, postFiles = None):
        self.url = url
        self.callback = callback
        self.errback = errback
        self.headerCallback = headerCallback
        self.bodyDataCallback = bodyDataCallback
        self.method = method
        self.start = start
        self.etag = etag
        self.modified = modified
        self.cookies = cookies # A dictionary of cookie names to
                               # dictionaries containing the keys
                               # 'Value', 'Version', 'received',
                               # 'Path', 'Domain', 'Port', 'Max-Age',
                               # 'Discard', 'Secure', and optionally
                               # one or more of the following:
                               # 'Comment', 'CommentURL', 'origPath',
                               # 'origDomain', 'origPort'
        self.postVariables = postVariables
        self.postFiles = postFiles
        self.depth = 0
        self.authAttempts = 0
        self.updateURLOk = True
        self.originalURL = self.updatedURL = self.redirectedURL = url
        self.userAgent = "%s/%s (%s)" % \
                         (config.get(prefs.SHORT_APP_NAME),
                          config.get(prefs.APP_VERSION),
                          config.get(prefs.PROJECT_URL))
        self.connection = None
        self.cancelled = False
        self.initHeaders()

    def __str__(self):
        return "%s: %s" % (type(self).__name__, self.url)

    def cancel(self):
        self.cancelled = True
        if self.connection is not None:
            self.connection.closeConnection()
            self.connection = None

    def isValidCookie(self, cookie, scheme, host, port, path):
        return ((time.time() - cookie['received'] < cookie['Max-Age']) and
                (cookie['Version'] == '1') and
                self.hostMatches(host, cookie['Domain']) and
                path.startswith(cookie['Path']) and
                self.portMatches(str(port), cookie['Port']) and
                (scheme == 'https' or not cookie['Secure']))

    def dropStaleCookies(self):
        """Remove cookies that have expired or are invalid for this URL"""
        scheme, host, port, path = parseURL(self.url)
        temp = {}
        for name in self.cookies:
            if self.isValidCookie(self.cookies[name], scheme, host, port, path):
                temp[name] = self.cookies[name]
        self.cookies = temp

    def hostMatches(self, host, host2):
        host = host.lower()
        host2 = host2.lower()
        if host.find('.') == -1:
            host = host+'.local'
        if host2.find('.') == -1:
            host2 = host2+'.local'
        if host2.startswith('.'):
            return host.endswith(host2)
        else:
            return host == host2

    def portMatches(self, port, portlist):
        if portlist is None:
            return True
        portlist = portlist.replace(',',' ').split()
        return port in portlist

    def initHeaders(self):
        self.headers = {}
        if self.start > 0:
            self.headers["Range"] = "bytes="+str(self.start)+"-"
        if not self.etag is None:
            self.headers["If-None-Match"] = self.etag
        if not self.modified is None:
            self.headers["If-Modified-Since"] = self.modified
        self.headers['User-Agent'] = self.userAgent
        self.setCookieHeader()

    def setCookieHeader(self):
        self.dropStaleCookies()
        if len(self.cookies) > 0:
            header = "$Version=1"
            for name in self.cookies:
                header = '%s;%s=%s' % (header,name,self.cookies[name]['Value'])
                if self.cookies[name].has_key('origPath'):
                    header = '%s;$Path=%s' % \
                                       (header,self.cookies[name]['origPath'])
                if self.cookies[name].has_key('origDomain'):
                    header = '%s;$Domain=%s' % \
                                       (header,self.cookies[name]['origDomain'])
                if self.cookies[name].has_key('origPort'):
                    header = '%s;$Port=%s' % \
                                       (header,self.cookies[name]['origPort'])
            self.headers['Cookie'] = header

    def startRequest(self):
        self.cancelled = False
        self.connection = None
        self.willHandleResponse = False
        self.gotBadStatusCode = False
        if 'Authorization' not in self.headers:
            scheme, host, port, path = parseURL(self.redirectedURL)
            def callback(authHeader):
                if self.cancelled:
                    error = RequestCanceledError()
                    self.errback(error)
                    return
                if authHeader is not None:
                    self.headers["Authorization"] = authHeader
                self.reallyStartRequest()
            httpauth.findHTTPAuth(callback, host, path)
        else:
            self.reallyStartRequest()

    def reallyStartRequest(self):
        if self.bodyDataCallback is not None:
            bodyDataCallback = self.onBodyData
        else:
            bodyDataCallback = None
        self.connectionPool.addRequest(self.callbackIntercept,
                self.errbackIntercept, self.onRequestStart, self.onHeaders,
                bodyDataCallback,
                self.url, self.method, self.headers, self.postVariables,
                self.postFiles)

    def statusCodeExpected(self, status):
        expectedStatusCodes = set([200])
        if self.start != 0:
            expectedStatusCodes.add(206)
        if self.etag is not None or self.modified is not None:
            expectedStatusCodes.add(304)
        return status in expectedStatusCodes

    def callbackIntercept(self, response):
        if self.cancelled:
            print "WARNING: Callback on a cancelled request for %s" % self.url
            import traceback
            traceback.print_stack()
            return
        if self.shouldRedirect(response):
            self.handleRedirect(response)
        elif self.shouldAuthorize(response):
            # FIXME: We reuse the id here, but if the request is
            # cancelled while the auth dialog is up, it won't actually
            # get cancelled.
            self.handleAuthorize(response)
        else:
            self.connection = None
            expectedStatusCodes = [200]
            if not self.gotBadStatusCode:
                if self.callback:
                    response = self.prepareResponse(response)
                    trapCall(self, self.callback, response)
            elif self.errback:
                error = UnexpectedStatusCode(response['status'])
                self.errbackIntercept(error)

    def errbackIntercept(self, error):
        if self.cancelled:
            return
        elif isinstance(error, PipelinedRequestNeverStarted):
            # Connection closed before our pipelined request started.  RFC
            # 2616 says we should retry
            self.startRequest() 
            # this should give us a new connection, since our last one closed
        elif (isinstance(error, ServerClosedConnection) and
                self.connection and 
                self.connection.requestsFinished > 0 and 
                self.connection.bytesRead == 0):
            # Connection closed when trying to reuse an http connection.  We
            # should retry with a fresh connection
            self.startRequest()
        else:
            self.connection = None
            trapCall(self, self.errback, error)

    def onRequestStart(self, connection):
        if self.cancelled:
            connection.closeConnection()
        else:
            self.connection = connection

    def onHeaders(self, response):
        if self.shouldRedirect(response) or self.shouldAuthorize(response):
            self.willHandleResponse = True
        else:
            if not self.statusCodeExpected(response['status']):
                self.gotBadStatusCode = True
            if self.headerCallback is not None:
                response = self.prepareResponse(response)
                if not trapCall(self, self.headerCallback, response):
                    self.cancel()

    def onBodyData(self, data):
        if (not self.willHandleResponse and not self.gotBadStatusCode and 
                self.bodyDataCallback):
            if not trapCall(self, self.bodyDataCallback, data):
                self.cancel()

    def prepareResponse(self, response):
        response['original-url'] = self.originalURL
        response['updated-url'] = self.updatedURL
        response['redirected-url'] = self.redirectedURL
        response['filename'] = self.getFilenameFromResponse(response)
        response['charset'] = self.getCharsetFromResponse(response)
        try:
            response['cookies'] = self.getCookiesFromResponse(response)
        except:
            print "ERROR in getCookiesFromResponse()"
            traceback.print_exc()
        return response

    def getCookiesFromResponse(self, response):
        """Generates a cookie dictionary from headers in response
        """
        def getAttrPair(attr):
            result = attr.strip().split('=',1)
            if len(result) == 2:
                (name, value) = result
            else:
                name = result[0]
                value = ''
            return (name, value)
        cookies = {}
        cookieStrings = []
        if response.has_key('set-cookie') or response.has_key('set-cookie2'):
            scheme, host, port, path = parseURL(self.redirectedURL)

            # Split header into cookie strings, respecting commas in
            # the middle of stuff
            if response.has_key('set-cookie'):
                cookieStrings.extend(response['set-cookie'].split(','))
            if response.has_key('set-cookie2'):
                cookieStrings.extend(response['set-cookie2'].split(','))
            temp = []
            for string in cookieStrings:
                if (len(temp) > 0 and (
                    (temp[-1].count('"')%2 == 1) or
                    (string.find('=') == -1) or
                    (string.find('=') > string.find(';')))):
                    temp[-1] = '%s,%s' % (temp[-1],string)
                else:
                    temp.append(string)
            cookieStrings = temp
            
            for string in cookieStrings:
                # Strip whitespace from the cookie string and split
                # into name-value pairs.
                string = string.strip()
                pairs = string.split(';')
                temp = []
                for pair in pairs:
                    if (len(temp) > 0 and
                        (temp[-1].count('"')%2 == 1)):
                        temp[-1] = '%s;%s' % (temp[-1],pair)
                    else:
                        temp.append(pair)
                pairs = temp

                (name, value) = getAttrPair(pairs.pop(0))
                cookie = {'Value' : value,
                          'Version' : '1',
                          'received' : time.time(),
                          # Path is everything up until the last /
                          'Path' : '/'.join(path.split('/')[:-1])+'/',
                          'Domain' : host,
                          'Port' : str(port),
                          'Secure' : False}
                for attr in pairs:
                    attr = attr.strip()
                    if attr.lower() == 'discard':
                        cookie['Discard'] = True
                    elif attr.lower() == 'secure':
                        cookie['Secure'] = True
                    elif attr.lower().startswith('version='):
                        cookie['Version'] = getAttrPair(attr)[1]
                    elif attr.lower().startswith('comment='):
                        cookie['Comment'] = getAttrPair(attr)[1]
                    elif attr.lower().startswith('commenturl='):
                        cookie['CommentURL'] = getAttrPair(attr)[1]
                    elif attr.lower().startswith('max-age='):
                        cookie['Max-Age'] = getAttrPair(attr)[1]
                    elif attr.lower().startswith('expires='):
                        now = time.time()
                        # FIXME: "expires" isn't very well defined and
                        # this code will probably puke in certain cases
                        try:
                            expires = time.mktime(time.strptime(
                                                getAttrPair(attr)[1],
                                              '%a, %d %b %Y %H:%M:%S %Z'))
                        except:
                            try:
                                expires = time.mktime(time.strptime(
                                              getAttrPair(attr)[1],
                                              '%a, %d-%b-%Y %H:%M:%S %Z'))
                            except:
                                print "DTV: Warning: Can't process cookie expiration: %s" % getAttrPair(attr)[1]
                                expires = 0
                        expires -= time.timezone
                        if expires < now:
                            cookie['Max-Age'] = 0
                        else:
                            cookie['Max-Age'] = int(expires - now)
                    elif attr.lower().startswith('domain='):
                        cookie['origDomain'] = getAttrPair(attr)[1]
                        cookie['Domain'] = cookie['origDomain']
                    elif attr.lower().startswith('port='):
                        cookie['origPort'] = getAttrPair(attr)[1]
                        cookie['Port'] = cookie['origPort']
                    elif attr.lower().startswith('path='):
                        cookie['origPath'] = getAttrPair(attr)[1]
                        cookie['Path'] = cookie['origPath']
                if not cookie.has_key('Discard'):
                    cookie['Discard'] = not cookie.has_key('Max-Age')
                if not cookie.has_key('Max-Age'):
                    cookie['Max-Age'] = str(2**30)
                if self.isValidCookie(cookie,scheme, host, port, path):
                    cookies[name] = cookie
        return cookies

    def findValueFromHeader(self, header, targetName):
        """Finds a value from a response header that uses key=value pairs with
        the ';' char as a separator.  This is how content-disposition and
        content-type work.
        """
        for part in header.split(';'):
            try:
                name, value = part.split("=", 1)
            except ValueError:
                pass
            else:
                if name.strip().lower() == targetName.lower():
                    return value.strip()
        return None

    def getFilenameFromResponse(self, response):
        try:
            disposition = response['content-disposition']
        except KeyError:
            pass
        else:
            filename = self.findValueFromHeader(disposition, 'filename')
            if filename is not None:
                return cleanFilename(filename)
        match = URIPattern.match(response['path'])
        if match is None:
            # This code path will never be executed.
            return cleanFilename(response['path'])
        filename = match.group(2)
        query = match.group(4)
        if not filename:
            ret = query
        elif not query:
            ret = filename
        else:
            ret = "%s-%s" % (filename, query)
        if ret is None:
            ret = 'unknown'
        return cleanFilename(ret)

    def getCharsetFromResponse(self, response):
        try:
            contentType = response['content-type']
        except KeyError:
            pass
        else:
            charset = self.findValueFromHeader(contentType, 'charset')
            if charset is not None:
                return charset
        return 'iso-8859-1'

    def shouldRedirect(self, response):
        return (response['status'] in (301, 302, 303, 307) and 
                self.depth < self.MAX_REDIRECTS and 
                'location' in response)

    def handleRedirect(self, response):
        self.depth += 1
        self.url = urljoin(self.url, response['location'])
        self.redirectedURL = self.url
        if response['status'] == 301 and self.updateURLOk:
            self.updatedURL = self.url
        else:
            self.updateURLOk = False
        if response['status'] == 303:
            # "See Other" we must do a get request for the result
            self.method = "GET"
            self.postVariables = None
        if 'Authorization' in self.headers:
            del self.headers["Authorization"]
        self.startRequest()

    def shouldAuthorize(self, response):
        return (response['status'] == 401 and 
                self.authAttempts < self.MAX_AUTH_ATTEMPS and
                'www-authenticate' in response)

    def handleAuthorize(self, response):
        match = re.search("(\w+)\s+realm\s*=\s*\"(.*?)\"$",
            response['www-authenticate'])
        if match is None:
            trapCall(self, self.errback, AuthorizationFailed())
            return
        authScheme = match.expand("\\1")
        realm = match.expand("\\2")
        if authScheme.lower() != 'basic':
            trapCall(self, self.errback, AuthorizationFailed())
            return
        def callback(authHeader):
            if authHeader is not None:
                self.headers["Authorization"] = authHeader
                self.authAttempts += 1
                self.startRequest()
            else:
                trapCall(self, self.errback, AuthorizationFailed())
        httpauth.askForHTTPAuth(callback, self.url, realm, authScheme)

def grabURL(url, callback, errback, headerCallback=None,
        bodyDataCallback=None, method="GET", start=0, etag=None,
        modified=None, cookies = {}, postVariables = None, postFiles = None):
    client = HTTPClient(url, callback, errback, headerCallback,
            bodyDataCallback, method, start, etag, modified, cookies, postVariables, postFiles)
    client.startRequest()
    return client

class HTTPHeaderGrabber(HTTPClient):
    """Modified HTTPClient to get the headers for a URL.  It tries to do a
    HEAD request, then falls back on doing a GET request, and closing the
    connection right after the headers.
    """

    def __init__(self, url, callback, errback):
        """HTTPHeaderGrabber support a lot less features than a real
        HTTPClient, mostly this is because they don't make sense in this
        context."""
        HTTPClient.__init__(self, url, callback, errback)
    
    def startRequest(self):
        self.method = "HEAD"
        HTTPClient.startRequest(self)

    def errbackIntercept(self, error):
        if self.method == 'HEAD' and not self.cancelled:
            self.method = "GET"
            HTTPClient.startRequest(self)
        else:
            HTTPClient.errbackIntercept(self, error)

    def callbackIntercept(self, response):
        # we send the callback for GET requests during the headers
        if self.method != 'GET' or self.willHandleResponse:
            HTTPClient.callbackIntercept(self, response)

    def onHeaders(self, headers):
        HTTPClient.onHeaders(self, headers)
        if (self.method == 'GET' and not self.willHandleResponse):
            headers['body'] = '' 
            # make it match the behaviour of a HEAD request
            self.callback(self.prepareResponse(headers))
            self.cancel()

def grabHeaders (url, callback, errback):
    client = HTTPHeaderGrabber(url, callback, errback)
    client.startRequest()
    return client

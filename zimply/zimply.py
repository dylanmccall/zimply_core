# pyZIM is a ZIM reader written entirely in Python 3.
# PyZIM takes its inspiration from the Internet in a Box project,
#  which can be seen in some of the main structures used in this project,
#  yet it has been developed independently and is not considered a fork
#  of the project. For more information on the Internet in a Box project,
#  do have a look at https://github.com/braddockcg/internet-in-a-box .


# Copyright (c) 2016, Kim Bauters
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
# ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# The views and conclusions contained in the software and documentation are those
# of the authors and should not be interpreted as representing official policies,
# either expressed or implied, of the FreeBSD Project.


import io
import logging
import lzma
import os
import pkg_resources
import re
import sqlite3
import time
import urllib
from collections import namedtuple
from functools import partial, lru_cache
from math import floor, pow, log
from struct import Struct, pack, unpack

# non-standard required packages are gevent and falcon (for its web server),
# and make (for templating)
from mako.template import Template
from gevent import monkey, pywsgi

# make sure to do the monkey-patching before loading the falcon package!
monkey.patch_all()
import falcon

verbose = False

logging.basicConfig(filename='zimply.log', filemode='w',
                    format="%(levelname)s: %(message)s",
                    level=logging.DEBUG if verbose else logging.INFO)

#####
# Definition of a number of basic structures/functions to simplify the code
#####

# define a standard zero which is used for zero terminated fields
ZERO = pack("B", 0)
# a field is a tuple consisting of its binary format and a name
Field = namedtuple("Field", ["format", "field_name"])
# an article is a triple
Article = namedtuple("Article", ["data", "namespace", "mimetype"])

iso639_3to1 = {"ara": "ar", "dan": "da", "nld": "nl", "eng": "en",
               "fin": "fi", "fra": "fr", "deu": "de", "hun": "hu",
               "ita": "it", "nor": "no", "por": "pt", "ron": "ro",
               "rus": "ru", "spa": "es", "swe": "sv", "tur": "tr"}


def read_zero_terminated(file, encoding):
    """
    Retrieve a ZERO terminated string by reading byte by byte until the ending
    ZERO is encountered.
    :param file: the file to read from
    :param encoding: the encoding used for the file
    :return: the decoded string, up to but not including the ZERO that
        terminated the string
    """
    # read until we find the ZERO termination
    buffer = iter(partial(file.read, 1), ZERO)
    # join all the bytes together
    field = b"".join(buffer)
    # transform the bytes into a string and return the string
    return field.decode(encoding=encoding, errors="ignore")


def convert_size(size):
    """
    Convert a given size in bytes to a human-readable string of the file size.
    :param size: the size in bytes
    :return: a human-readable string of the size
    """
    size_name = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
    power = int(floor(log(size, 1024)))
    base = pow(1024, power)
    size = round(size/base, 2)
    return '%s %s' % (size, size_name[power])


#####
# Description of the structure of a ZIM file, as of early 2016
# The full definition can be found on http://www.openzim.org/wiki/ZIM_file_format .
#####

HEADER = [  # define the HEADER structure of a ZIM file
    Field("I", "magicNumber"),
    Field("I", "version"),
    Field("Q", "uuid_low"),
    Field("Q", "uuid_high"),
    Field("I", "articleCount"),
    Field("I", "clusterCount"),
    Field("Q", "urlPtrPos"),
    Field("Q", "titlePtrPos"),
    Field("Q", "clusterPtrPos"),
    Field("Q", "mimeListPos"),
    Field("I", "mainPage"),
    Field("I", "layoutPage"),
    Field("Q", "checksumPos")
]

ARTICLE_ENTRY = [  # define the ARTICLE ENTRY structure of a ZIM file
    Field("H", "mimetype"),
    Field("B", "parameterLen"),
    Field("c", "namespace"),
    Field("I", "revision"),
    Field("I", "clusterNumber"),
    Field("I", "blobNumber")
    # zero terminated url
    # zero terminated title
    # variable length parameter data as per parameterLen
]

REDIRECT_ENTRY = [  # define the REDIRECT ENTRY structure of a ZIM file
    Field("H", "mimetype"),
    Field("B", "parameterLen"),
    Field("c", "namespace"),
    Field("I", "revision"),
    Field("I", "redirectIndex")
    # zero terminated url
    # zero terminated title
    # variable length parameter data as per parameterLen
]

CLUSTER = [  # define the CLUSTER structure of a ZIM file
    Field("B", "compressionType")
]


#####
# The internal classes used to easily access the different structures in a ZIM file.
#####

class Block:
    def __init__(self, structure, encoding):
        self._structure = structure
        self._encoding = encoding
        # Create a new Struct object to correctly read the binary data in this
        # block in particular, pass it along that it is a little endian (<),
        # along with all expected fields.
        self._compiled = Struct("<" + "".join(
            [field.format for field in self._structure]))
        self.size = self._compiled.size

    def unpack(self, buffer, offset=0):
        # Use the Struct to read the binary data in the buffer where this block
        # appears at the given offset.
        values = self._compiled.unpack_from(buffer, offset)
        # Match up each value with the corresponding field in the block and put
        # it in a dictionary for easy reference.
        return {field.field_name: value for value, field in
                zip(values, self._structure)}

    def _unpack_from_file(self, file, offset=None):
        if offset is not None:
            # move the pointer in the file to the specified offset;
            # this is not index 0
            file.seek(offset)
        # read in the amount of data corresponding to the block size
        buffer = file.read(self.size)
        # return the values of the fields after unpacking them
        return self.unpack(buffer)

    def unpack_from_file(self, file, seek=None):
        # When more advanced behaviour is needed, this method can be overridden.
        return self._unpack_from_file(file, seek)


class HeaderBlock(Block):
    def __init__(self, encoding):
        super().__init__(HEADER, encoding)


class MimeTypeListBlock(Block):
    def __init__(self, encoding):
        super().__init__("", encoding)

    def unpack_from_file(self, file, offset=None):
        # move the pointer in the file to the specified offset; this is not index 0
        if offset is not None:
            file.seek(offset)
        mimetypes = []  # prepare an empty list to store the mimetypes
        while True:
            # get the next zero terminated field
            s = read_zero_terminated(file, self._encoding)
            mimetypes.append(s)  # add the newly found mimetype to the list
            if s == "":  # the last entry must be an empty string
                mimetypes.pop()  # pop the last entry
                return mimetypes  # return the list of mimetypes we found


class ClusterBlock(Block):
    def __init__(self, encoding):
        super().__init__(CLUSTER, encoding)


@lru_cache(maxsize=32)  # provide an LRU cache for this object
class ClusterData(object):
    def __init__(self, file, offset, encoding):
        self.file = file  # store the file
        self.offset = offset  # store the offset
        cluster_info = ClusterBlock(encoding).unpack_from_file(
            self.file, self.offset)  # Get the cluster fields.
        # Verify whether the cluster has LZMA2 compression
        self.compressed = cluster_info['compressionType'] == 4
        # at the moment, we don't have any uncompressed data
        self.uncompressed = None
        self._decompress()  # decompress the contents as needed
        # Prepare storage to keep track of the offsets of the blobs in the
        # cluster.
        self._offsets = []
        # proceed to actually read the offsets of the blobs in this cluster
        self._read_offsets()

    def _decompress(self, chunk_size=32768):
        if self.compressed:
            # create a bytes stream to store the uncompressed cluster data
            self.buffer = io.BytesIO()
            decompressor = lzma.LZMADecompressor()  # get the decompressor ready
            # move the file pointer to the start of the blobs as long as we
            # don't reach the end of the stream.
            self.file.seek(self.offset + 1)

            while not decompressor.eof:
                chunk = self.file.read(chunk_size)  # read in a chunk
                data = decompressor.decompress(chunk)  # decompress the chunk
                self.buffer.write(data)  # and store it in the buffer area

    def _source_buffer(self):
        # get the file buffer or the decompressed buffer
        buffer = self.buffer if self.compressed else self.file
        # move the buffer to the starting position
        buffer.seek(0 if self.compressed else self.offset + 1)
        return buffer

    def _read_offsets(self):
        # get the buffer for this cluster
        buffer = self._source_buffer()
        # read the offset for the first blob
        offset0 = unpack("<I", buffer.read(4))[0]
        # store this one in the list of offsets
        self._offsets.append(offset0)
        # calculate the number of blobs by dividing the first blob by 4
        number_of_blobs = int(offset0 / 4)
        for idx in range(number_of_blobs - 1):
            # store the offsets to all other blobs
            self._offsets.append(unpack("<I", buffer.read(4))[0])

    def read_blob(self, blob_index):
        # check if the blob falls within the range
        if blob_index >= len(self._offsets) - 1:
            raise IOError("Blob index exceeds number of blobs available: %s" %
                          blob_index)
        buffer = self._source_buffer()  # get the buffer for this cluster
        # calculate the size of the blob
        blob_size = self._offsets[blob_index+1] - self._offsets[blob_index]
        # move to the position of the blob relative to current position
        buffer.seek(self._offsets[blob_index], 1)
        return buffer.read(blob_size)


class DirectoryBlock(Block):
    def __init__(self, structure, encoding):
        super().__init__(structure, encoding)

    def unpack_from_file(self, file, seek=None):
        # read in the first set of fields as defined in the ARTICLE_ENTRY structure
        field_values = super()._unpack_from_file(file, seek)
        # then read in the url, which is a zero terminated field
        field_values["url"] = read_zero_terminated(file, self._encoding)
        # followed by the title, which is again a zero terminated field
        field_values["title"] = read_zero_terminated(file, self._encoding)
        field_values["namespace"] = field_values["namespace"].decode(
            encoding=self._encoding, errors="ignore")
        return field_values


class ArticleEntryBlock(DirectoryBlock):
    def __init__(self, encoding):
        super().__init__(ARTICLE_ENTRY, encoding)


class RedirectEntryBlock(DirectoryBlock):
    def __init__(self, encoding):
        super().__init__(REDIRECT_ENTRY, encoding)


#####
# Support functions to simplify (1) the uniform creation of a URL given a
# namespace, and (2) searching in the index.
#####

def full_url(namespace, url):
    return str(namespace) + '/' + str(url)


def binary_search(func, item, front, end):
    logging.debug("performing binary search with boundaries " + str(front) +
                  " - " + str(end))
    found = False
    middle = 0

    # continue as long as the boundaries don't cross and we haven't found it
    while front < end and not found:
        middle = floor((front + end) / 2)  # determine the middle index
        # use the provided function to find the item at the middle index
        found_item = func(middle)
        if found_item == item:
            found = True  # flag it if the item is found
        else:
            if found_item < item:  # if the middle is too early ...
                # move the front index to the middle
                # (+ 1 to make sure boundaries can be crossed)
                front = middle + 1
            else:  # if the middle falls too late ...
                # move the end index to the middle
                # (- 1 to make sure boundaries can be crossed)
                end = middle - 1

    return middle if found else None


class ZIMFile:
    '''
    The main class to access a ZIM file, offering two important public methods:
        get_article_by_url(...) is used to retrieve an article given its
            namespace and url.

        get_main_page() is used to retrieve the article representing the
            main page for this ZIM file.
    '''
    def __init__(self, filename, encoding):
        self._enc = encoding
        # open the file as a binary file
        self.file = open(filename, "rb")
        # retrieve the header fields
        self.header_fields = HeaderBlock(self._enc).unpack_from_file(self.file)
        self.mimetype_list = MimeTypeListBlock(self._enc).unpack_from_file(
            self.file, self.header_fields["mimeListPos"])
        # create the object once for easy access
        self.redirectEntryBlock = RedirectEntryBlock(self._enc)

        self.articleEntryBlock = ArticleEntryBlock(self._enc)
        self.clusterFormat = ClusterBlock(self._enc)

    def _read_offset(self, index, field_name, field_format, length):
        # move to the desired position in the file
        self.file.seek(self.header_fields[field_name] + int(length * index))

        # and read and return the particular format
        return unpack("<" + field_format, self.file.read(length))[0]

    def _read_url_offset(self, index):
        return self._read_offset(index, "urlPtrPos", "Q", 8)

    def _read_title_offset(self, index):
        return self._read_offset(index, "titlePtrPos", "L", 4)

    def _read_cluster_offset(self, index):
        return self._read_offset(index, "clusterPtrPos", "Q", 8)

    def _read_directory_entry(self, offset):
        '''
        returns a DirectoryBlock - either Article Entry or Redirect Entry
        '''
        logging.debug("reading entry with offset " + str(offset))

        self.file.seek(offset)  # move to the desired offset

        # retrieve the mimetype to determine the type of block
        fields = unpack("<H", self.file.read(2))

        # get block class
        if fields[0] == 0xffff:
            directory_block = self.redirectEntryBlock
        else:
            directory_block = self.articleEntryBlock
        # unpack and return the desired Directory Block
        return directory_block.unpack_from_file(self.file, offset)

    def read_directory_entry_by_index(self, index):
        '''
        returns DirectoryBlock - either Article Entry or Redirect Entry
        '''
        # find the offset for the given index
        offset = self._read_url_offset(index)
        # read the entry at that offset
        directory_values = self._read_directory_entry(offset)
        # set the index in the list of values
        directory_values["index"] = index
        return directory_values  # and return all these directory values

    def _read_blob(self, cluster_index, blob_index):
        # get the cluster offset
        offset = self._read_cluster_offset(cluster_index)
        # get the actual cluster data
        cluster_data = ClusterData(self.file, offset, self._enc)
        # return the data read from the cluster at the given blob index
        return cluster_data.read_blob(blob_index)

    def _get_article_by_index(self, index, follow_redirect=True):
        # get the info from the DirectoryBlock at the given index
        entry = self.read_directory_entry_by_index(index)
        if 'redirectIndex' in entry.keys():  # check if we have a Redirect Entry
            # if we follow up on redirects, return the article it is pointing to
            if follow_redirect:
                logging.debug("redirect to " + str(entry['redirectIndex']))
                return self._get_article_by_index(entry['redirectIndex'],
                                                  follow_redirect)
            # otherwise, simply return no data and provide the redirect index
            # as the metadata.
            else:
                return Article(None, entry['namespace'], entry['redirectIndex'])
        else:  # otherwise, we have an Article Entry
            # get the data and return the Article
            data = self._read_blob(entry['clusterNumber'], entry['blobNumber'])
            return Article(data, entry['namespace'],
                           self.mimetype_list[entry['mimetype']])

    def _get_entry_by_url(self, namespace, url, linear=False):
        if linear:  # if we are performing a linear search ...
            # ... simply iterate over all articles
            for idx in range(self.header_fields['articleCount']):
                # get the info from the DirectoryBlock at that index
                entry = self.read_directory_entry_by_index(idx)
                # if we found the article ...
                if entry['url'] == url and entry['namespace'] == namespace:
                    # return the DirectoryBlock entry and the index of the entry
                    return entry, idx
            # return None, None if we could not find the entry
            return None, None
        else:
            front = middle = 0
            end = len(self)
            title = full_url(namespace, url)
            logging.debug("performing binary search with boundaries " +
                          str(front) + " - " + str(end))
            found = False
            # continue as long as the boundaries don't cross and
            # we haven't found it
            while front <= end and not found:
                middle = floor((front + end) / 2)  # determine the middle index
                entry = self.read_directory_entry_by_index(middle)
                logging.debug("checking " + entry['url'])
                found_title = full_url(entry['namespace'], entry['url'])
                if found_title == title:
                    found = True  # flag it if the item is found
                else:
                    if found_title < title:  # if the middle is too early ...
                        # move the front index to middle
                        # (+ 1 to ensure boundaries can be crossed)
                        front = middle + 1
                    else:  # if the middle falls too late ...
                        # move the end index to middle
                        # (- 1 to ensure boundaries can be crossed)
                        end = middle - 1
            if found:
                return (self.read_directory_entry_by_index(middle), middle)
            return (None, None)

    def get_article_by_url(self, namespace, url, follow_redirect=True):
        entry, idx = self._get_entry_by_url(namespace, url)  # get the entry
        if idx: # We found an index and return the Article.
            return self._get_article_by_index(
                idx, follow_redirect=follow_redirect)  # and article
        return None

    def get_main_page(self):
        '''
        Get the main page based on its index
        '''
        return self._get_article_by_index(self.header_fields['mainPage'])

    def metadata(self):
        '''
        Retrieves the metadata and returns it as a dict with
        the entry url as key and the metadata as value as an instance of the
        Article class.
        '''
        metadata = {}
        # iterate backwards over the entries
        for i in range(self.header_fields['articleCount'] - 1, -1, -1):
            entry = self.read_directory_entry_by_index(i)  # get the entry
            if entry['namespace'] == 'M':  # check that it is still metadata
                # turn the key to lowercase as per Kiwix standards
                m_name = entry['url'].lower()
                # get the data, which is encoded as an article
                metadata[m_name] = self._get_article_by_index(i)[0]
            else:  # stop as soon as we are no longer looking at metadata
                break
        return metadata

    def __len__(self):  # retrieve the number of articles in the ZIM file
        return self.header_fields['articleCount']

    def __iter__(self):
        '''
        Return an iterator closure that gives the articles
        one by one (URL, title, and index)
        '''
        for idx in range(self.header_fields['articleCount']):
            # get the Directory Entry
            entry = self.read_directory_entry_by_index(idx)
            if entry['namespace'] == "A":
                # add the full url to the entry
                entry['fullUrl'] = full_url(entry['namespace'], entry['url'])
                yield entry['fullUrl'], entry['title'], idx

    def close(self):
        self.file.close()

    def __exit__(self, *_):
        '''
        Ensure a proper close of the file when the ZIM file object is destroyed.
        '''
        self.close()


#####
# a BM25 ranker, used to determine the score of results returned in search queries.
#####


class BM25:
    # see https://en.wikipedia.org/wiki/Okapi_BM25 for information on
    # the Best Match 25 algorithm.

    def __init__(self, k1=1.2, b=0.75):
        self.k1 = k1  # set the k1 ...
        self.b = b  # ... and b free parameter

    def calculate_scores(self, query, corpus):
        '''
        Calculate the BM25 scores for all the documents in the corpus given
        the query.
        :param query: a tuple containing the words that were looking for.
        :param corpus: a list of strings, each string corresponding to a result
        returned based on the query.
        :return: a list of scores (higher is better), in the same order as the
        documents in the corpus.
        '''

        corpus_size = len(corpus)  # the total number of documents in the corpus
        query = [term.lower() for term in query]  # turn the query itself into lowercase
        # also turn each document into lowercase
        corpus = [document.lower() for document in corpus]

        # Determine the average number of words in each document
        # (simply count the number of spaces) store them in a dict with
        # the hash of the document as the key and the amount of words as the
        # value.
        num_words = dict((hash(doc), doc.count(" ") + 1) for doc in corpus)
        avg_doc_len = sum(num_words.values()) / len(corpus)
        query_terms = []

        for term in query:
            frequency = 0  # assume this term does not occur in any document
            for document in corpus:
                # ... and tally up the number of documents the term occurs in
                frequency += 1 if document.find(term) > -1 else 0
            query_terms.append((term, frequency))

        result = []  # prepare a list to keep the resulting scores
        # we are now ready to calculate the score of each document in the corpus
        for document in corpus:
            total_score = 0
            for term_frequency in query_terms:  # for every term ...
                # ... get the term and its frequency
                term, frequency = term_frequency
                # determine the IDF score (numerator and denominator swapped
                # to achieve a positive score)
                idf = log((frequency + 0.5) / (corpus_size - frequency + 0.5))

                # count how often the term occurs in the document itself
                doc_frequency = document.count(term)
                num_words_doc = num_words[hash(document)]
                doc_k1 = doc_frequency * (self.k1 + 1)
                doc_b = doc_frequency + self.k1 * 1 - self.b + self.b * \
                         (num_words_doc / avg_doc_len)
                total_score += idf * (doc_k1 / doc_b)
            # once the score for all terms is summed up,
            # add this score to the result list
            result.append(total_score)

        return result


#####
# The supporting classes to provide the HTTP server. This includes the template
#  and the actual request handler that uses the ZIM file to retrieve the desired page, images, CSS, etc.
#####

class ZIMRequestHandler:
    # provide for a class variable to store the ZIM file object
    zim = None
    # provide a class variable to store the index file
    reverse_index = None
    # provide another class variable to store the schema for the index file
    schema = None
    # store the location of the template file in a class variable
    template = None
    # the encoding, stored in a class variable, for the ZIM file contents
    encoding = ""

    def on_get(self, request, response):
        '''
        Process a HTTP GET request. An object is this class is created whenever
        an HTTP request is generated. This method is triggered when the request
        is of any type, typically a GET. This method will redirect the user,
        based on the request, to the index/search/correct page, or an error
        page if the resource is unavailable.
        '''

        location = request.relative_uri
        # replace the escaped characters by their corresponding string values
        location = urllib.parse.unquote(location)
        components = location.split("?")
        navigation_location = None
        is_article = True  # assume an article is requested, unless we find out otherwise
        # if trying for the main page ...
        if location in ["/", "/index.htm", "/index.html", "/main.htm", "/main.html"]:
            # ... return the main page as the article
            article = ZIMRequestHandler.zim.get_main_page()
            navigation_location = "main"
        else:
            # The location is given as domain.com/namespace/url/parts/ ,
            # as used in the ZIM links or, alternatively, as domain.com/page.htm
            # , as the user would input
            _, namespace, *url_parts = location.split("/")  # noqa
            # we are dealing with a request from the address bar, e.g.
            # /article_name.htm
            if len(namespace) > 1:
                url = namespace  # the namespace is then the URL
                namespace = "A"  # and the namespace is an article
            else:
                # combine all the url parts together again
                url = "/".join(url_parts)
            # get the desired article
            article = ZIMRequestHandler.zim.get_article_by_url(namespace, url)
            # we have an article when the namespace is A
            # (i.e. not a photo, etc.)
            is_article = (namespace == "A")

        # from this point forward, "article" refers to an element in the ZIM
        # database whereas is_article refers to a Boolean to indicate whether
        # the "article" is a true article, i.e. a webpage
        success = True  # assume the request succeeded unless we find out otherwise
        search = False  # assume we do not have a search, unless we find out otherwise
        keywords = ""  # the keywords to search for

        if not article and len(components) <= 1:
            success = False  # there is no article to be retrieved, and there is no ? in the URI to indicate a search
        elif len(components) > 1:  # check if we have a URI of the form main?arguments
            arguments = components.pop()  # retrieve the arguments part by popping the top of the sequence
            if arguments.find("q=") == 0:  # check if arguments starts with ?q= to indicate a proper search
                search = True  # if so, we have a search
                navigation_location = "search"  # also update the navigation location
                arguments = re.sub(r"^q=", r"", arguments)  # remove the q= at the start
                keywords = arguments.split("+")  # combine all keywords by joining them together with +'s
            else:  # if the required ?q= is not found at the start ...
                success = False  # this is not a search, and hence we did not find the requested resource

        template = Template(filename=ZIMRequestHandler.template)
        result = body = head = title = ""  # preset the result and all variables used in the template
        if success:  # if we achieved success, i.e. we found the requested resource
            response.status = falcon.HTTP_200  # respond with a success code
            response.content_type = "text/HTML" if search else article.mimetype  # set the right content type
            if not navigation_location:  # check if the article location is already set
                    navigation_location = "browse"  # if not default to "browse" to indicate a non-search, non-main page

            if not search:  # if we did not have a search from but a plain "article"
                if is_article:
                    content = article.data  # we have an actual article, i.e. a webpage
                    content = content.decode(encoding=ZIMRequestHandler.encoding)  # decode its contents into a string
                    # retrieve the body from the ZIM article
                    m = re.search(r"<body.*?>(.*?)</body>", content, re.S)
                    body = m.group(1) if m else ""
                    # retrieve the head from the ZIM article
                    m = re.search(r"<head.*?>(.*?)</head>", content, re.S)
                    head = m.group(1) if m else ""
                    # retrieve the title from the ZIM article
                    m = re.search(r"<title.*?>(.*?)</title>", content, re.S)
                    title = m.group(1) if m else ""
                    logging.info("accessing the article: " + title)
                else:
                    # just a binary blob, so use it as such
                    result = article.data
            else:  # if we did have a search form
                # show the search query in the title
                title = "search results for >> " + " ".join(keywords)
                logging.info("searching for the keywords >> " + " ".join(keywords))
                # qp = QueryParser("title", schema=ZIMRequestHandler.schema)  # load the parser for the given schema
                # q = qp.parse(" ".join(keywords))  # use the keywords to search the index

                cursor = ZIMRequestHandler.reverse_index.cursor()
                search_for = "* ".join(keywords) + "*"
                cursor.execute(
                    '''SELECT docid FROM papers WHERE title MATCH ? ''',
                    [search_for])

                results = cursor.fetchall()
                if not results:
                    body = "no results found for: " + " <i>" + " ".join(
                        keywords) + "</i>"  # ... let the user know
                else:
                    titles = []
                    for row in results:  # ... iterate over all the results
                        # abuse an internal function to read the directory
                        # entry by index (rather than e.g. URL)
                        entry = self.zim.read_directory_entry_by_index(row[0])
                        # add the full url to the entry
                        # url = entry['url']
                        # body += "<a href=\"/" + url + "\" >" + entry['title'] + "</a><br />"  # ... show its link
                        titles.append((entry['title'], entry['url']))

                    bm25 = BM25()
                    scores = bm25.calculate_scores(
                        keywords, [title[0] for title in titles])
                    weighted = zip(scores, titles)
                    weighted_result = sorted(weighted, reverse=True)

                    for _, (title, url) in weighted_result:
                        body += '<a href="{}">{}</a><br />'.format(url, title)

        else:  # if we did not achieve success
            response.status = falcon.HTTP_404
            response.content_type = "text/HTML"
            title = "Page 404"
            body = "requested resource not found"

        if not result:  # if the result hasn't been prefilled ...
            result = template.render(location=navigation_location, body=body,
                                     head=head, title=title)  # render template
            response.data = bytes(result, encoding=ZIMRequestHandler.encoding)
        else:
            # if result is already filled, push it through as-is
            # (i.e. binary resource)
            response.data = result


class ZIMServer:
    def __init__(self, filename, index_file="",
                 template=pkg_resources.resource_filename(
                     __name__, 'template.html'),
                 port=9454, encoding="utf-8"):
        # create the object to access the ZIM file
        self._zim_file = ZIMFile(filename, encoding)
        # get the language of the ZIM file and convert it to ISO639_1 or
        # default to "en" if unsupported
        default_iso = bytes("eng", encoding=encoding)
        iso639 = self._zim_file.metadata().get("language", default_iso) \
            .decode(encoding=encoding, errors="ignore")
        lang = iso639_3to1.get(iso639, "en")
        logging.info("A ZIM file in the language " + str(lang) + " (ISO639-1) was found, " +
                     "containing " + str(len(self._zim_file)) + " articles.")
        index_file = os.path.join(os.path.dirname(filename), "index.idx") if not index_file else index_file
        logging.info("The index file is determined to be located at " + str(index_file) + ".")

        # set this object to a class variable of ZIMRequestHandler
        ZIMRequestHandler.zim = self._zim_file
        # set the index schema to a class variable of ZIMRequestHandler
        # ZIMRequestHandler.schema = self._schema
        # set (and create) the index to a class variable
        ZIMRequestHandler.reverse_index = self._bootstrap(index_file)
        # set the template to a class variable of ZIMRequestHandler
        ZIMRequestHandler.template = template
        # set the encoding to a class variable of ZIMRequestHandler
        ZIMRequestHandler.encoding = encoding

        app = falcon.API()
        main = ZIMRequestHandler()
        # create a simple sync that forwards all requests; TODO: only allow GET
        app.add_sink(main.on_get, prefix='/')
        print("up and running on http://localhost:" + str(port))
        # start up the HTTP server on the desired port
        pywsgi.WSGIServer(("", port), app).serve_forever()

    def _bootstrap(self, index_file):
        if not os.path.exists(index_file):  # check whether the index exists
            logging.info("No index was found at " + str(index_file) +
                         ", so now creating the index.")
            print("Please wait as the index is created, this can take quite some time! – " + time.strftime('%X %x'))

            db = sqlite3.connect(index_file)
            cursor = db.cursor()
            # limit memory usage to 64MB
            cursor.execute('''PRAGMA CACHE_SIZE = -65536''')
            # create a contentless virtual table using full-text search (FTS4)
            # and the porter tokeniser
            cursor.execute('''CREATE VIRTUAL TABLE papers USING fts4(content="", title, tokenize=porter);''')
            # get an iterator to access all the articles
            articles = iter(self._zim_file)

            for url, title, idx in articles:  # retrieve the articles one by one
                cursor.execute(
                    '''INSERT INTO papers(docid, title) VALUES (?, ?)''',
                    (idx, title))  # and add them
            # once all articles are added, commit the changes to the database
            db.commit()

            print("Index created, continuing – " + time.strftime('%X %x'))
            db.close()
        # return an open connection to the SQLite database
        return sqlite3.connect(index_file)

    def __exit__(self, *_):
        self._zim_file.close()

# to start a ZIM server using ZIMply, all you need to provide is the location of the ZIM file:
# server = ZIMServer("wiki.zim")

# alternatively, you can specify your own location for the index, use a custom template, or change the port:
# server = ZIMServer("wiki.zim", "index.idx", "template.html", 80)

# all arguments can also be named, so you can also choose to simply change the port:
# server = ZIMServer("wiki.zim", port=80)

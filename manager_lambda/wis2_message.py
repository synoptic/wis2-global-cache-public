import base64
import hashlib
import io
import json
import os
import traceback
import urllib
from copy import deepcopy
from enum import Enum
from uuid import uuid4
from datetime import datetime as dt
import requests
import boto3
import shutil

def nested_get(d, keys):
    """
    gets value of nested key/s in dict
    :param d: dict
    :param keys: list of keys
    :return: value of nested key
    """
    for key in keys:
        try:
            d = d[key]
        except KeyError:
            # print(f'key "{key}" not found in {d}')
            return None
    return d


# class to represent each WIS2 message
class Wis2Message:
    # not using json schema validation at this time, but could
    # https://github.com/wmo-im/wis2-notification-message/blob/main/schemas/wis2-notification-message-bundled.json
    def __init__(self, msg_data: dict, env: dict = None):
        self.pubtime_epoch = None
        self.env = env
        self.msg = msg_data
        self.init_parse()
        self.new_uuid = uuid4().__str__()
        self.new_topic = self.topic.replace('origin', 'cache')
        self.is_valid = None
        self.content_encodings = [
            "utf-8",
            "base64",
            "gzip"
        ]
        self.dataserver = None
        self.src_link = None

    def init_parse(self):
        """
        Initializes the parsing of the WIS2 message and sets required attributes.

        Raises:
            Exception: If any of the required keys are missing in the message.
        """
        required_keys = {
            'id': ['id'],
            'data_id': ['properties', 'data_id'],
            # 'metadata_id': ['properties', 'metadata_id'],
            'topic': ['topic'],
            'links': ['links'],
            'pubtime': ['properties', 'pubtime']
        }
        for k, v in required_keys.items():
            prop_val = nested_get(self.msg, v)
            if prop_val is None:
                raise Exception(f"required key {k} missing in message {self.msg}")
            setattr(self, k, prop_val)
        setattr(self, 'integrity_block', nested_get(self.msg, ['properties', 'integrity']))
        # setattr(self, "unique_id", self.data_id + self.pubtime)
        setattr(self, 'do_cache', self.check_cache())
        try:
            self.pubtime_epoch = dt.strptime(self.pubtime, '%Y-%m-%dT%H:%M:%SZ').timestamp()
        except:
            # limit decimal places
            if len(self.pubtime.split(':')) == 4:
                # then the last colon should be a decimal - replace with period
                self.pubtime = self.pubtime.rsplit(':', 1)[0] + '.' + self.pubtime.rsplit(':', 1)[1]
            dt_parts = self.pubtime.split('.')
            dt_seconds = dt_parts[1]
            if len(dt_seconds) > 4:
                dt_seconds = dt_seconds[:3]+"Z"
            new_dt = ".".join([dt_parts[0], dt_seconds])
            self.pubtime_epoch = dt.strptime(new_dt, '%Y-%m-%dT%H:%M:%S.%fZ').timestamp()

    def get_source_link(self) -> str:
        """Extract source link from message and set related attributes.

        Returns:
            Source URL href string.

        Raises:
            TypeError: If no canonical or update link found.
            ValueError: If URL cannot be parsed.
        """
        # Find appropriate link
        canonical_link = [link for link in self.links if link['rel'] == 'canonical']
        update_link = [link for link in self.links if link['rel'] == 'update']
        src_link = update_link[0] if update_link else canonical_link[0] if canonical_link else None

        if not src_link:
            raise TypeError("missing canonical or update link")

        link_href = src_link['href']

        # basic sanity check
        if not link_href or not link_href.strip():
            raise ValueError(f"Empty URL in message {self.data_id}")

        # extract components
        try:
            parsed = urllib.parse.urlparse(link_href)
            # handle edge cases gracefully
            path = parsed.path.rstrip('/')
            filename = path.split('/')[-1] if path else ''

            # Log warning for edge cases but don't fail
            if not filename:
                print(f"Warning: Could not extract filename from URL {link_href} in message {self.data_id}")
                filename = 'unknown'

            if not parsed.netloc:
                raise ValueError(f"Invalid URL structure (no hostname): {link_href}")

            # Set attributes
            self.src_link = src_link
            self.filename = urllib.parse.unquote(filename)  # Decode percent-encoded filenames
            self.dataserver = parsed.netloc

            return link_href

        except Exception as e:
            # Add context for debugging
            raise ValueError(f"Failed to parse URL for message {self.data_id}: {e}") from e

    def check_cache(self):
        """
        checks if the message should be cached
        -------
        bool - whether to cache the message
        """
        # check if cache property exists and or is set
        cache_msg_value = nested_get(self.msg, ['properties', 'cache'])
        if cache_msg_value is None: cache_msg_value = True
        if not cache_msg_value or cache_msg_value == 'false':
            return False
        else:
            return True

    def is_unique(self, last_cache):
        """
        determines if the message should be processed
        Parameters
        ----------
        last_cache - float - epoch time of last cache or None
        Returns
        -------
        bool - whether to cache the message
        """
        # check if cache property exists
        if last_cache is None:
            return True
        last_cache = float(last_cache)
        if self.pubtime_epoch <= last_cache:
            return False
        else:
            # check if update link exists
            if any([link for link in self.links if link['rel'] == 'update']):
                return True
            else:
                return False
        return True

    def cache_msg_data(self, use_content: bool = False):
        """
        caches the message data
        """
        dnld_link = self.get_source_link()
        dndld_keys = {'content': ['properties', 'content', 'value'],
                      'encoding': ['properties', 'content', 'encoding'],
                      'size': ['properties', 'content', 'size']}
        for k, v in dndld_keys.items():
            prop_val = nested_get(self.msg, v)
            setattr(self, k, prop_val)
        data_bytes = None
        if use_content and self.content:
            if self.encoding not in self.content_encodings:
                raise Exception(f"unknown encoding {self.encoding} for {self.data_id}")
            elif self.encoding == 'base64':
                data_bytes = base64.b64decode(self.content)
            elif self.encoding == 'utf-8':
                data_bytes = self.content.encode()
            else:
                raise Exception(f"unsupported encoding {self.encoding} for {self.data_id}")

        else:
            data_file = self.download_file(dnld_link)
            with open(data_file, "rb") as file:
                data_bytes = file.read()
        # set attribute
        setattr(self, 'data_bytes', data_bytes)
        # set integrity block if missing in the msg
        self.set_integrity_block(data_bytes)
        return data_bytes

    # function to set the integrity block if it is missing from the message
    def set_integrity_block(self, data_bytes: bytes):
        """
        sets the integrity block if it is missing from the message
        """
        if self.integrity_block is None:
            # set the integrity block
            sh = hashlib.sha512()
            sh.update(data_bytes)
            b64_digest = base64.b64encode(sh.digest()).decode()
            # hex_digest = sh.hexdigest()
            self.msg['properties']['integrity'] = self.integrity_block = {
                "method": "sha512",
                "value": b64_digest
            }
        return self.integrity_block

    def format_cache_msg(self):
        # message is the same, except for the msg_id (id is new uuid), links (new canonical link),
        # and change the topic channel to cache
        # "type" (mimetype) should be retained from the original message.

        cache_msg = self.msg
        # update msg_id
        cache_msg['id'] = self.new_uuid
        # drop the topic
        try:
            cache_msg.pop('topic')
        except:
            pass
        if self.do_cache:
            # overwrite the canonical link and update link if it exists
            for link_type in ['canonical', 'update']:
                links = [link for link in cache_msg['links'] if link['rel'] == link_type]
                if links:
                    original_link = cache_msg['links'].pop(cache_msg['links'].index(links[0]))
                    new_link = deepcopy(original_link)
                    new_link['href'] = self.dnld_url
                    cache_msg['links'].append(new_link)
        return cache_msg

    @staticmethod
    def get_dt_str():
        # RFC3339
        """
        gets current datetime in RFC3339 format
        Returns - str of current datetime in RFC3339 format
        -------

        """
        return dt.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')

    def download_file(self, href: str, tmp_dir: str = '/tmp/'):
        """
        downloads file from url to filename
        Parameters
        ----------
        href - str - url to download from
        tmp_dir - str - directory to save to

        Returns
        -------
        tmp_path - path to downloaded file
        """
        session = requests.Session()
        # Configure limited retries to fail faster
        retries = requests.packages.urllib3.util.retry.Retry(
            total=2,  # Only retry once
            backoff_factor=0.5,  # Short delay between retries
            status_forcelist=[500, 502, 503, 504]  # Only retry on server errors
        )

        # Apply configuration to both HTTP and HTTPS connections
        session.mount('http://', requests.adapters.HTTPAdapter(max_retries=retries))
        session.mount('https://', requests.adapters.HTTPAdapter(max_retries=retries))

        dev_mode = os.environ.get('DEV-MODE', 'False') not in ['True', 'true', '1', True]
        tmp_path = os.path.join(tmp_dir, self.filename)
        try:
            # timeout=(10, 30) means: 10s connection timeout, 30s read timeout
            with session.get(href, stream=True, timeout=(10, 30), verify=not dev_mode) as r:
                r.raise_for_status()
                expected_size = int(r.headers.get('content-length', 0))
                downloaded_size = 0

                # Make sure /tmp has enough space for larger files
                if expected_size > 0:
                    free_space = shutil.disk_usage('/tmp').free
                    if free_space < expected_size:
                        raise IOError(f"Not enough space in /tmp for file of size {expected_size} bytes. Free space: {free_space} bytes")
                with open(tmp_path, 'wb') as f:
                    # Increase chunk size for better performance with larger files
                    for chunk in r.iter_content(chunk_size=32768):  # 32KB chunks
                        if chunk:
                            f.write(chunk)
                            downloaded_size += len(chunk)

                # Verify download completeness for files with reported size
                # too aggressive...
                # if expected_size > 0 and downloaded_size != expected_size:
                #     raise IOError(f"Downloaded size ({downloaded_size}) doesn't match expected size ({expected_size})")

                # set attribute for deletion later
                setattr(self, 'tmp_path', tmp_path)
                return tmp_path
        except requests.exceptions.RequestException as e:
            print(f"Failed to download file from {href}: {e}")
            # Clean up partial download
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
        except (IOError, OSError) as e:
            print(f"I/O error while downloading {href}: {e}")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def validate_integrity(self):
        """
        validates the data
        """
        input_bytes = self.data_bytes
        valid_methods = {
            "sha256": hashlib.sha256,
            "sha384": hashlib.sha384,
            "sha512": hashlib.sha512,
            "sha3-256": hashlib.sha3_256,
            "sha3-384": hashlib.sha3_384,
            "sha3-512": hashlib.sha3_512
        }
        method = self.integrity_block["method"]
        if method not in valid_methods:
            raise Exception(f"Unsupported hashing method: {method}")

        sh = valid_methods[method]()
        sh.update(input_bytes)
        b64_digest = base64.b64encode(sh.digest()).decode()
        hex_digest = sh.hexdigest()
        if self.integrity_block["value"] not in [b64_digest, hex_digest]:
            setattr(self, 'is_valid', False)
            raise Exception(f"checksum failed for: {self.data_id}")
        setattr(self, 'is_valid', True)
        return True

    def upload_to_bucket(self, data_bytes: bytes):
        # get s3 client
        # include a /data prefix for all cached objects
        data_prefix = 'data'
        s3_client_obj = boto3.client('s3')
        topic_pieces = self.topic.split('/')
        # get wis2 index from topic_pieces
        topic_prefix = "/".join(topic_pieces[topic_pieces.index('wis2') + 1:])
        bucket_path = "/".join([data_prefix, topic_prefix, self.filename])
        setattr(self, 'bucket_path', bucket_path)
        # construct download url
        dnld_url = os.path.join(f"https://{self.env['s3_bucket_name']}.s3.amazonaws.com",
                                bucket_path)
        setattr(self, 'dnld_url', dnld_url)

        if os.environ.get('DEV-MODE', 'False') in ['True', 'true', '1']:
            print(f"dev no upload: {self.bucket_path}")
            return self.bucket_path
        s3_client_obj.upload_fileobj(Fileobj=io.BytesIO(data_bytes), Bucket=self.env['s3_bucket_name'],
                                     Key=self.bucket_path)
        return self.bucket_path


class InvalidUpload(Exception):
    """The request itself is wrong (bad extension, corrupt zip, too large). Maps to HTTP 400."""


class ProcessingError(Exception):
    """The upload looked fine but could not be read as geospatial data.

    The file is still recorded, with status FAILED and this message as the error.
    """

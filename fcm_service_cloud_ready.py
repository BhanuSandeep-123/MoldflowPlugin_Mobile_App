"""backend/fcm_service_cloud_ready.py
----------------------------------
Backward-compatibility shim.
Authoritative implementation is in backend/fcm_service.py.
"""
from fcm_service import (  # noqa: F401
    UnregisteredDeviceError,
    send_fcm_notification,
    _load_service_account,
    _get_access_token,
    _validate_credentials,
)

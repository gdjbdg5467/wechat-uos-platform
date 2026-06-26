"""Monkey-patch itchat send_raw_msg to log the response."""
import itchat.components.messages as msg_mod
import logging

logger = logging.getLogger(__name__)
_original_send_raw_msg = msg_mod.send_raw_msg

def _patched_send_raw_msg(self, msgType, content, toUserName):
    url = '%s/webwxsendmsg' % self.loginInfo.get('url', 'NO_URL')
    r = _original_send_raw_msg(self, msgType, content, toUserName)
    try:
        resp = r.get('BaseResponse', {})
        ret = resp.get('Ret', '?')
        err = resp.get('ErrMsg', '?')
        logger.warning("=== SEND RAW MSG === url=%s to=%s type=%s Ret=%s ErrMsg=%s content_len=%d",
                      url[:50], toUserName[:20] if toUserName else 'None', msgType, ret, str(err)[:50], len(str(content)))
        if ret != 0:
            logger.error("SEND FAILED: Ret=%s ErrMsg=%s to=%s", ret, err, toUserName[:20])
    except Exception as e:
        logger.warning("Failed to log send result: %s", e)
    return r

msg_mod.send_raw_msg = _patched_send_raw_msg
logger.warning("=== itchat send_raw_msg monkey-patched ===")

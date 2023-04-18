import time

import web

import channel.wechatmp.receive as receive
import channel.wechatmp.reply as reply
from bridge.context import *
from channel.wechatmp.common import *
from channel.wechatmp.wechatmp_channel import WechatMPChannel
from common.log import logger
from config import conf


# This class is instantiated once per query
class Query:
    def GET(self):
        return verify_server(web.input())

    def POST(self):
        try:
            request_time = time.time()
            channel = WechatMPChannel()
            webData = web.data()
            logger.debug("[wechatmp] Receive post data:\n" + webData.decode("utf-8"))
            wechatmp_msg = receive.parse_xml(webData)
            if wechatmp_msg.msg_type == "text" or wechatmp_msg.msg_type == "voice":
                from_user = wechatmp_msg.from_user_id
                to_user = wechatmp_msg.to_user_id
                message = wechatmp_msg.content.decode("utf-8")
                message_id = wechatmp_msg.msg_id

                supported = True
                if "【收到不支持的消息类型，暂无法显示】" in message:
                    supported = False  # not supported, used to refresh

                # New request
                if (
                    from_user not in channel.cache_dict
                    and from_user not in channel.running
                    or message.startswith("#") 
                    and message_id not in channel.request_cnt # insert the godcmd
                ):
                    # The first query begin
                    context = channel._compose_context(
                        ContextType.TEXT, message, isgroup=False, msg=wechatmp_msg
                    )
                    logger.debug(
                        "[wechatmp] context: {} {}".format(context, wechatmp_msg)
                    )

                    if supported and context:
                        # set private openai_api_key
                        # if from_user is not changed in itchat, this can be placed at chat_channel
                        user_data = conf().get_user_data(from_user)
                        context["openai_api_key"] = user_data.get("openai_api_key")
                        channel.running.add(from_user)
                        channel.produce(context)
                    else:
                        trigger_prefix = conf().get("single_chat_prefix", [""])
                        if trigger_prefix or not supported:
                            if trigger_prefix:
                                content = textwrap.dedent(
                                    f"""\
                                    请输入'{trigger_prefix}'接你想说的话跟我说话。
                                    例如:
                                    {trigger_prefix}你好，很高兴见到你。"""
                                )
                            else:
                                content = textwrap.dedent(
                                    """\
                                    你好，很高兴见到你。
                                    请跟我说话吧。"""
                                )
                        else:
                            logger.error(f"[wechatmp] unknown error")
                            content = textwrap.dedent(
                                """\
                                未知错误，请稍后再试"""
                            )
                        replyPost = reply.TextMsg(wechatmp_msg.from_user_id, wechatmp_msg.to_user_id, content).send()
                        return replyPost


                # Wechat official server will request 3 times (5 seconds each), with the same message_id.
                # Because the interval is 5 seconds, here assumed that do not have multithreading problems.
                request_cnt = channel.request_cnt.get(message_id, 0) + 1
                channel.request_cnt[message_id] = request_cnt
                logger.info(
                    "[wechatmp] Request {} from {} {}\n{}\n{}:{}".format(
                        request_cnt,
                        from_user,
                        message_id,
                        message,
                        web.ctx.env.get("REMOTE_ADDR"),
                        web.ctx.env.get("REMOTE_PORT"),
                    )
                )

                task_running = True
                waiting_until = request_time + 4
                while time.time() < waiting_until:
                    if from_user in channel.running:
                        time.sleep(0.1)
                    else:
                        task_running = False
                        break

                reply_text = ""
                if task_running:
                    if request_cnt < 3:
                        # waiting for timeout (the POST request will be closed by Wechat official server)
                        time.sleep(2)
                        # and do nothing, waiting for the next request
                        return "success"
                    else: # request_cnt == 3:
                        # return timeout message
                        reply_text = "【正在思考中，回复任意文字尝试获取回复】"
                        # replyPost = reply.TextMsg(from_user, to_user, reply_text).send()
                        # return replyPost

                # reply or reply_text is ready
                channel.request_cnt.pop(message_id)

                # no return because of bandwords or other reasons
                if (
                    from_user not in channel.cache_dict
                    and from_user not in channel.running
                ):
                    return "success"

                # reply is ready
                if from_user in channel.cache_dict:
                    # Only one message thread can access to the cached data
                    try:
                        content = channel.cache_dict.pop(from_user)
                    except KeyError:
                        return "success"

                    if len(content.encode("utf8")) <= MAX_UTF8_LEN:
                        reply_text = content
                    else:
                        continue_text = "\n【未完待续，回复任意文字以继续】"
                        splits = split_string_by_utf8_length(
                            content,
                            MAX_UTF8_LEN - len(continue_text.encode("utf-8")),
                            max_split=1,
                        )
                        reply_text = splits[0] + continue_text
                        channel.cache_dict[from_user] = splits[1]

                logger.info(
                    "[wechatmp] Request {} do send to {} {}: {}\n{}".format(
                        request_cnt,
                        from_user,
                        message_id,
                        message,
                        reply_text,
                    )
                )
                replyPost = reply.TextMsg(from_user, to_user, reply_text).send()
                return replyPost

            elif wechatmp_msg.msg_type == "event":
                logger.info(
                    "[wechatmp] Event {} from {}".format(
                        wechatmp_msg.content, wechatmp_msg.from_user_id
                    )
                )
                content = subscribe_msg()
                replyMsg = reply.TextMsg(
                    wechatmp_msg.from_user_id, wechatmp_msg.to_user_id, content
                )
                return replyMsg.send()
            else:
                logger.info("暂且不处理")
                return "success"
        except Exception as exc:
            logger.exception(exc)
            return exc

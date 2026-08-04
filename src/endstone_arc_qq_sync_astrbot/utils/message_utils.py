"""
消息处理工具模块
用于处理QQ消息的emoji表情符号和CQ码转换
"""

import re
from typing import Optional, Tuple

# Hub 子服命令路由：编号上限（含）
HUB_MAX_NUMERIC_SERVER_ID = 64

# QQ 群消息在游戏内的虚拟来源服（地球 Online 梗）
QQ_GROUP_VIRTUAL_SERVER_NAME = "地球Online服务器"

# Minecraft Java 遗留格式：U+00A7（§）后跟一个格式/颜色字符
_mc_format_code_pattern = re.compile("\u00a7.")


def strip_minecraft_format_codes(text: str) -> str:
    """移除 Minecraft § 格式码（§ 及其后一个字符），用于转发到 QQ 等纯文本场景。"""
    if not text:
        return text
    return _mc_format_code_pattern.sub("", text)


def parse_hub_command_routing(raw_message: str) -> Tuple[str, Optional[int]]:
    """
    从群内命令中解析 Hub 子服目标编号（可选）。
    返回 (去掉编号后的完整命令行, 子服编号或 None 表示全部子服)。

    规则（内部形式，由弧光中枢剥掉 /mc 后下发）：
    - /list /tps /info /banlist /help /reload：末尾单独的数字为子服编号；
    - /who：倒数第一个参数为编号（需至少两个参数）；
    - /cmd：第一个参数为纯数字时为子服编号，其后为控制台命令；
    """
    s = (raw_message or "").strip()
    if not s.startswith("/"):
        return s, None
    parts = s.split()
    if not parts:
        return s, None

    head = parts[0]
    cmd = head[1:].lower() if head.startswith("/") else head.lower()
    args = parts[1:]

    def _valid_sid(token: str) -> Optional[int]:
        if not token.isdigit():
            return None
        sid = int(token)
        if 1 <= sid <= HUB_MAX_NUMERIC_SERVER_ID:
            return sid
        return None

    # /cmd <子服编号> <控制台命令...>
    if cmd == "cmd" and args:
        sid = _valid_sid(args[0])
        if sid is not None:
            rest = [parts[0]] + args[1:]
            return " ".join(rest), sid

    trailing_sid_cmds = {
        "list",
        "tps",
        "info",
        "banlist",
        "help",
        "reload",
    }
    if cmd in trailing_sid_cmds and args:
        sid = _valid_sid(args[-1])
        if sid is not None:
            return " ".join(parts[:-1]), sid

    if cmd == "who" and len(args) >= 2:
        sid = _valid_sid(args[-1])
        if sid is not None:
            return " ".join(parts[:-1]), sid

    return s, None


def remove_emoji_for_game(text):
    """
    将emoji表情符号转换为文本描述，供游戏内显示使用
    """
    if not text:
        return text
    
    # 常见emoji映射表
    emoji_map = {
        '😀': '[笑脸]', '😁': '[开心]', '😂': '[笑哭]', '🤣': '[大笑]', '😃': '[微笑]',
        '😄': '[开心]', '😅': '[汗笑]', '😆': '[眯眼笑]', '😉': '[眨眼]', '😊': '[微笑]',
        '😋': '[流口水]', '😎': '[酷]', '😍': '[花眼]', '😘': '[飞吻]', '🥰': '[三颗心]',
        '😗': '[亲吻]', '😙': '[亲吻]', '😚': '[亲吻]', '☺': '[微笑]', '🙂': '[微笑]',
        '🤗': '[拥抱]', '🤩': '[星眼]', '🤔': '[思考]', '🤨': '[怀疑]', '😐': '[面无表情]',
        '😑': '[无语]', '😶': '[无言]', '🙄': '[白眼]', '😏': '[坏笑]', '😣': '[困扰]',
        '😥': '[失望]', '😮': '[惊讶]', '🤐': '[闭嘴]', '😯': '[惊讶]', '😪': '[困倦]',
        '😫': '[疲倦]', '😴': '[睡觉]', '😌': '[安心]', '😛': '[吐舌]', '😜': '[眨眼吐舌]',
        '😝': '[闭眼吐舌]', '🤤': '[流口水]', '😒': '[无聊]', '😓': '[冷汗]', '😔': '[沮丧]',
        '😕': '[困惑]', '🙃': '[倒脸]', '🤑': '[财迷]', '😲': '[震惊]', '☹': '[皱眉]',
        '🙁': '[皱眉]', '😖': '[困扰]', '😞': '[失望]', '😟': '[担心]', '😤': '[愤怒]',
        '😢': '[流泪]', '😭': '[大哭]', '😦': '[皱眉]', '😧': '[痛苦]', '😨': '[害怕]',
        '😩': '[疲倦]', '🤯': '[爆头]', '😬': '[咧嘴]', '😰': '[冷汗]', '😱': '[尖叫]',
        '🥵': '[热]', '🥶': '[冷]', '😳': '[脸红]', '🤪': '[疯狂]', '😵': '[晕]',
        '😡': '[愤怒]', '😠': '[生气]', '🤬': '[咒骂]', '😷': '[口罩]', '🤒': '[生病]',
        '🤕': '[受伤]', '🤢': '[恶心]', '🤮': '[呕吐]', '🤧': '[喷嚏]', '😇': '[天使]',
        '🥳': '[庆祝]', '🥺': '[请求]', '🤠': '[牛仔]', '🤡': '[小丑]', '🤥': '[说谎]',
        '🤫': '[嘘]', '🤭': '[捂嘴笑]', '🧐': '[单片眼镜]', '🤓': '[书呆子]',
        
        # 手势
        '👍': '[赞]', '👎': '[踩]', '👌': '[OK]', '✌': '[胜利]', '🤞': '[交叉手指]',
        '🤟': '[爱你]', '🤘': '[摇滚]', '🤙': '[打电话]', '👈': '[左指]', '👉': '[右指]',
        '👆': '[上指]', '👇': '[下指]', '☝': '[食指]', '✋': '[举手]', '🤚': '[举手背]',
        '🖐': '[张开手]', '🖖': '[瓦肯礼]', '👋': '[挥手]', '🤛': '[左拳]', '🤜': '[右拳]',
        '👊': '[拳头]', '✊': '[拳头]', '👏': '[拍手]', '🙌': '[举双手]', '👐': '[张开双手]',
        '🤲': '[捧手]', '🙏': '[祈祷]', '✍': '[写字]', '💪': '[肌肉]',
        
        # 心形
        '❤': '[红心]', '🧡': '[橙心]', '💛': '[黄心]', '💚': '[绿心]', '💙': '[蓝心]',
        '💜': '[紫心]', '🖤': '[黑心]', '🤍': '[白心]', '🤎': '[棕心]', '💔': '[心碎]',
        '❣': '[心叹号]', '💕': '[两颗心]', '💞': '[旋转心]', '💓': '[心跳]', '💗': '[增长心]',
        '💖': '[闪亮心]', '💘': '[心箭]', '💝': '[心礼盒]', '💟': '[心装饰]',
        
        # 常用符号
        '🔥': '[火]', '💯': '[100分]', '💢': '[愤怒]', '💥': '[爆炸]', '💫': '[星星]',
        '💦': '[汗滴]', '💨': '[风]', '🕳': '[洞]', '💣': '[炸弹]', '💤': '[睡觉]',
        '👀': '[眼睛]', '🗨': '[对话框]', '💭': '[思考泡泡]',
        
        # 动物（常见的）
        '🐶': '[小狗]', '🐱': '[小猫]', '🐭': '[老鼠]', '🐹': '[仓鼠]', '🐰': '[兔子]',
        '🦊': '[狐狸]', '🐻': '[熊]', '🐼': '[熊猫]', '🐨': '[考拉]', '🐯': '[老虎]',
        '🦁': '[狮子]', '🐮': '[牛]', '🐷': '[猪]', '🐽': '[猪鼻]', '🐸': '[青蛙]',
        '🐵': '[猴脸]', '🙈': '[非礼勿视]', '🙉': '[非礼勿听]', '🙊': '[非礼勿言]',
    }
    
    # 替换已知的emoji
    result = text
    for emoji, description in emoji_map.items():
        result = result.replace(emoji, description)
    
    # 使用正则表达式移除其他 unicode emoji（含彩色圆点 🟢🔴 等 U+1F7E0 段）
    emoji_pattern = re.compile(
        '['
        '\U0001F600-\U0001F64F'  # 表情符号
        '\U0001F300-\U0001F5FF'  # 符号和象形文字
        '\U0001F680-\U0001F6FF'  # 交通和地图符号
        '\U0001F7E0-\U0001F7FF'  # 几何图形扩展（圆点、方块等）
        '\U0001F1E0-\U0001F1FF'  # 国旗
        '\U00002600-\U000026FF'  # 杂项符号
        '\U00002700-\U000027BF'  # 装饰符号
        '\U0001F900-\U0001F9FF'  # 补充符号和象形文字
        '\U0001FA70-\U0001FAFF'  # 符号和象形文字扩展-A
        '\U00002300-\U000023FF'  # 杂项技术符号
        '\U0001F000-\U0001F02F'  # 麻将符号
        '\U0001F0A0-\U0001F0FF'  # 扑克符号
        '\U0000FE0F'            # 变体选择符-16（部分 emoji 序列）
        ']+',
        flags=re.UNICODE
    )
    
    # 将未映射的emoji替换为[表情]
    result = emoji_pattern.sub('[表情]', result)
    
    return result


def text_for_minecraft_display(text: str) -> str:
    """游戏内一行文本：去掉 § 颜色码，并将 emoji 转为可显示的说明（避免基岩版乱码）。"""
    if not text:
        return text
    t = strip_minecraft_format_codes(text)
    return remove_emoji_for_game(t)


def parse_qq_message(message_data):
    """
    解析QQ消息，将非文本内容转换为对应的标识符
    
    Args:
        message_data (dict): QQ消息数据
        
    Returns:
        str: 处理后的消息文本
    """
    
    # 获取原始消息文本
    raw_message = message_data.get("raw_message", "")
    
    if raw_message:
        # 使用正则表达式解析CQ码
        def replace_cq_code(match):
            cq_type = match.group(1)
            if cq_type == "image":
                return "[图片]"
            elif cq_type == "video":
                return "[视频]"
            elif cq_type == "record":
                return "[语音]"
            elif cq_type == "face":
                return "[表情]"
            elif cq_type == "at":
                # 提取@的QQ号
                params = match.group(2)
                if "qq=all" in params:
                    return "@全体成员"
                else:
                    qq_match = re.search(r'qq=(\d+)', params)
                    if qq_match:
                        return f"@{qq_match.group(1)}"
                    return "@某人"
            elif cq_type == "reply":
                return "[回复]"
            elif cq_type == "forward":
                return "[转发]"
            elif cq_type == "file":
                return "[文件]"
            elif cq_type == "share":
                return "[分享]"
            elif cq_type == "location":
                return "[位置]"
            elif cq_type == "music":
                return "[音乐]"
            elif cq_type == "xml" or cq_type == "json":
                return "[卡片]"
            else:
                return "[非文本]"
        
        # 匹配CQ码格式: [CQ:type,param1=value1,param2=value2]
        cq_pattern = r'\[CQ:([^,\]]+)(?:,([^\]]*))?\]'
        processed_message = re.sub(cq_pattern, replace_cq_code, raw_message)
        
        # 处理emoji表情符号，转换为游戏内可显示的文本
        processed_message = remove_emoji_for_game(processed_message)
        
        # 如果处理后的消息不为空，返回处理结果
        if processed_message.strip():
            return processed_message.strip()
    
    # 如果都没有内容，返回空消息标识
    return "[空消息]"


def clean_message_text(text: str) -> str:
    """
    清理消息文本，移除不必要的字符
    
    Args:
        text (str): 原始文本
        
    Returns:
        str: 清理后的文本
    """
    if not text:
        return text
    
    # 移除多余的空白字符
    text = re.sub(r'\s+', ' ', text.strip())
    
    # 移除控制字符
    text = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', text)
    
    return text


def truncate_message(message: str, max_length: int = 500) -> str:
    """
    截断过长的消息
    
    Args:
        message (str): 原始消息
        max_length (int): 最大长度
        
    Returns:
        str: 截断后的消息
    """
    if not message or len(message) <= max_length:
        return message
    
    return message[:max_length - 3] + "..."


def filter_sensitive_content(text: str, custom_ban_words=None) -> tuple:
    """No-op: keyword filtering disabled; return original text.

    Args:
        text: Original text.
        custom_ban_words: Ignored; kept for call-site compatibility.

    Returns:
        Tuple of (text, False).
    """
    _ = custom_ban_words
    return text, False


def format_qq_group_chat_game_message(
    display_name: str,
    message: str,
    group_name: str = "",
) -> str:
    """
    将 QQ 群聊天格式化为游戏内跨服聊天样式，来源显示为「地球Online服务器」。
    """
    from endstone import ColorFormat

    from_server = QQ_GROUP_VIRTUAL_SERVER_NAME
    if group_name and str(group_name).strip():
        from_server = f"{from_server}·{str(group_name).strip()}"

    server_label = text_for_minecraft_display(from_server)
    player = text_for_minecraft_display(str(display_name))
    msg = text_for_minecraft_display(str(message))

    return (
        f"{ColorFormat.GRAY}[跨服|{server_label}] {ColorFormat.AQUA}{player}"
        f"{ColorFormat.GRAY}: {ColorFormat.WHITE}{msg}"
    )


def format_cross_server_event_game_message(data: dict, local_server_name: str):
    """
    将 cross_server_event 负荷格式化为游戏内一行广播文案。
    本机为事件来源服时返回 None（避免重复显示）；无需上屏时亦返回 None。
    """
    from endstone import ColorFormat

    raw_from_server = data.get("from_server", "未知")
    if raw_from_server == local_server_name:
        return None

    from_server = text_for_minecraft_display(str(raw_from_server))
    event = data.get("event")
    player = text_for_minecraft_display(str(data.get("player", "")))
    message = text_for_minecraft_display(str(data.get("message", "")))

    if event == "chat":
        return (
            f"{ColorFormat.GRAY}[跨服|{from_server}] {ColorFormat.AQUA}{player}"
            f"{ColorFormat.GRAY}: {ColorFormat.WHITE}{message}"
        )
    if event == "join":
        return (
            f"{ColorFormat.GRAY}[跨服|{from_server}] {ColorFormat.GREEN}{player} 加入了游戏"
        )
    if event == "quit":
        return (
            f"{ColorFormat.GRAY}[跨服|{from_server}] {ColorFormat.RED}{player} 离开了游戏"
        )
    if event == "death":
        # message is usually the full QQ death line from ARCCore.
        body = message or (f"{player} 死了" if player else "有玩家死了")
        return (
            f"{ColorFormat.GRAY}[跨服|{from_server}] {ColorFormat.YELLOW}{body}"
        )
    if event in ("server_start", "server_connected"):
        return (
            f"{ColorFormat.GRAY}[跨服] {ColorFormat.GREEN}服务器 [{from_server}] 已启动"
        )
    if event in ("server_stop", "server_disconnected"):
        return (
            f"{ColorFormat.GRAY}[跨服] {ColorFormat.RED}服务器 [{from_server}] 已停止"
        )
    if event == "custom":
        return (
            f"{ColorFormat.GRAY}[跨服|{from_server}] {ColorFormat.WHITE}{message}"
        )
    return None

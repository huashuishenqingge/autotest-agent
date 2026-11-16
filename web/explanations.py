"""Chinese reading guides based on frozen requirements and saved outcomes.

These are editorial explanations, not model replies or new correctness evidence.
"""

TASK_GUIDES = {
    'dateutil_relativedelta___add': ('日期：午夜0点也算填写了时间', '0点是有效的小时值，不能和None（未指定小时）混为一谈。原先的真假判断可能误把0当成没填。', '原时间是下午13点，指定hour=0后应变成当天午夜0点；不指定小时才应保留13点。'),
    'dateutil_relativedelta___eq': ('日期：默认星期序号的比较', '未填写星期序号时按默认的第1次处理；比较时还要保留0、None和1的既有兼容行为。', 'MO表示默认的星期一，MO(1)表示第1个星期一；不能只因一种写了序号、另一种没写就判为不同。'),
    'dateutil_relativedelta__fix': ('日期：秒数进位', '相对时间到60秒时应进位为1分钟，秒数归零；还要兼顾负数及其他时间分量。', 'seconds=60应归一为minutes=1、seconds=0；59秒还不应进位。'),
    'itsdangerous_serializer_iter_unsigners': ('签名：空盐值也算明确指定', '明确传入空盐值时应使用这个空值；只有None才选择配置中的默认盐值。', '传salt=b""和传salt=None含义不同，不能都套用默认盐值。'),
    'itsdangerous_signer_derive_key': ('签名：空密钥不能当作没传', '明确传入空字节密钥与None不同；在none派生方式下应返回明确传入的密钥。', '传入b""时不能偷偷改用默认密钥，虽然空字节在真假判断中为假。'),
    'jinja_do_striptags': ('模板：按HTML协议取内容再去标签', '对象提供__html__时，应对它返回的HTML去标签，而非对普通str表示去标签。', '同一对象的__html__返回<b>你好</b>，普通字符串表示为“对象”；去标签应得到“你好”。'),
    'markupsafe_markup___add': ('安全字符串：不支持的拼接应报错', '字符串与不支持的整数拼接时应遵循类型协议，不能悄悄丢掉整数。', '安全字符串加整数不能直接返回原字符串；最终应按运算协议报TypeError或走合法的反向运算。'),
    'markupsafe_markup___getitem': ('安全字符串：下标0取第一个字符', '索引行为应与普通字符串一致，并保留Markup安全字符串类型。', '对“abc”取下标0应得到“a”，而不是从第2个字符开始。'),
    'markupsafe_markup___html_format': ('安全字符串：不支持的格式必须报错', 'HTML格式说明非空时应抛出ValueError；空格式与非空格式不能混淆。', '空格式是允许的；传入不支持的非空格式时不能直接返回原内容蒙混过关。'),
    'markupsafe_markup_split': ('安全字符串：最多切分0次', 'maxsplit=0表示不做分隔切分，应遵循普通字符串对应参数的语义。', '以逗号切分“a,b”且maxsplit=0时，应保留为一项“a,b”。'),
    'v2_click_datetime_convert': ('命令行：已有日期对象不变成文字', '输入已经是datetime对象时，应保留日期对象类型，而不是转为字符串。', '传入datetime对象，返回结果仍须是datetime，后续才能正常进行日期运算。'),
    'v2_click_pacifyflushwrapper_flush': ('命令行：只忽略管道断开错误', '刷新输出时只抑制EPIPE（管道已断开）；其他OSError必须继续抛出。', '接收端关闭导致EPIPE可以忽略；磁盘或其他系统错误不能被一起吞掉。'),
    'v2_click_stringparamtype_convert': ('命令行：坏字节也要返回字符串', '字符串参数转换最终应返回str；无法正常解码的字节应按既有替换解码规则处理。', '传入包含非法编码的bytes时，可以出现替换字符，但不能直接返回bytes。'),
    'v2_click_uuidparametertype_convert': ('命令行：已有UUID不变成文字', '输入已经是UUID对象时，转换后仍应保留UUID类型。', '传入一个UUID对象，不应返回看似一样但类型变成str的文字。'),
    'v2_dateutil_rrulebase_xafter': ('日期：最多取0次就不应产生结果', 'count=0表示最多产生0个日期；None才表示不设该数量上限。', '要求0个重复日期，结果应为空，不能因为0在真假判断中为假就当作无限数量。'),
    'v2_jinja_do_dictsort': ('模板：排序字段只接受指定选项', 'by只能选key或value，其他值应抛出FilterArgumentError。', '传by="other"应明确报错，不能自动猜测按键还是按值排序。'),
    'v2_jinja_do_round': ('模板：不支持的取整方式应报错', 'method不在支持的取整方式中时，应抛出FilterArgumentError，而不是原样返回数值。', '传一个拼错的method，调用者应收到错误，而非以为取整已经完成。'),
    'v2_jinja_do_truncate': ('模板：刚好达到长度时不截断', '当文本长度恰好等于指定长度且leeway=0时，不应截断或加省略号。', '文本长度5、限制5、宽限0时，应保留完整5个字符。'),
    'v2_jinja_sync_do_sum': ('模板：索引0是有效的求和字段', 'attribute=0表示取每项的第一个元素求和，不能当成没有指定attribute。', '[(1,2),(3,4)]按索引0求和，应计算1+3，而不是把整个元组拿来相加。'),
    'v2_markupsafe_markup_escape': ('安全字符串：转义后保留子类', '从Markup子类调用escape时，应返回正确的子类对象，同时保留正常转义规则。', '自定义SafeText子类调用escape，结果不能悄悄退回普通Markup类型。'),
    'v2_markupsafe_markup_format_map': ('安全字符串：格式化后保留安全类型', 'format_map的结果应仍为Markup类型，避免丢失已转义字符串的类型信息。', '填充模板后文字看起来一样还不够，结果类型也应保留为安全字符串。'),
    'v3_click_textwrapper__handle_long_word': ('命令行：长单词也要遵守每行宽度', '启用长词拆分且宽度为1时，每行应最多容纳1个字符。', '宽度1下处理长单词abc，应拆成单字符行，而不是整词挤在同一行。'),
    'v3_click_textwrapper_indent_only': ('命令行：第二行使用后续缩进', '原始段落首行使用initial_indent，第二行起应使用subsequent_indent。', '首行缩进2格、后续行4格时，第2行不能继续沿用2格缩进。'),
    'v3_itsdangerous_signer_verify_signature': ('签名：畸形签名不能验证通过', '格式不合法的base64签名字节不能被当作合法签名接受。', '传入畸形签名时应验证失败，不能因为异常处理过宽而直接返回True。'),
    'v3_jinja_do_indent': ('模板：空字符串默认不添加空格', '默认缩进操作不应在空字符串的空首行或空白行上平白增加空格。', '对空字符串使用默认缩进后，不能突然得到几个空格。'),
    'v4_dateutil_parsererror___str': ('日期：异常信息本身也必须能打印', '异常格式串与参数不匹配时，ParserError仍应遵循异常字符串协议保持可打印。', '原本就有日期解析错误，打印错误消息时不能再因格式化失败冒出第二个错误。'),
    'v5_itsdangerous_hmacalgorithm_get_signature': ('签名：空消息按标准HMAC计算', '空消息也是合法输入，应按标准HMAC算法处理，不能替换成一个零字节。', '空消息b""和包含一个零字节的b"\\x00"不同，不能用后者代替前者。'),
    'v5_itsdangerous_signer_get_signature': ('签名：空消息的签名不能被偷换', '空消息签名应是派生密钥对该空消息计算标准HMAC后，再进行URL安全base64编码。', '空消息也走规定的计算和编码步骤，不能换个占位内容来计算。'),
    'v6_dateutil_validate_fromutc_inputs': ('日期：拒绝时区对象不匹配', '输入datetime的tzinfo不是接收方时区对象时，应抛ValueError，且不应调用被包装的转换函数。', '把属于另一个时区对象的时间直接交给当前时区fromutc，应在转换前拒绝。'),
    'v6_itsdangerous_badheader___init': ('签名：保留原始异常对象', 'BadHeader应保留调用者传入的original_error异常对象，方便继续诊断错误来源。', '把一个底层异常传进去后，应能通过original_error拿回同一个对象，而非仅拿到它的文字。'),
}

GLOSSARY = [
    ('rationale','生成理由：模型为什么设计这些测试。'),
    ('diagnosis','问题诊断：模型认为哪里有错、为什么要改；它仍可能判断错误。'),
    ('files / path / content','文件列表 / 文件位置 / 文件内容：模型提出的代码，不是执行命令。'),
    ('replacements / qualified_name','函数替换提案 / 函数名称：建议替换哪个函数及其完整内容。'),
    ('task_id / base_revision','任务编号 / 基线版本：用于防止把回复应用到错误的任务或旧版本。'),
    ('based_on_run_id','诊断依据的失败运行编号：说明修复基于哪次测试结果。'),
    ('assert','测试中的检查：条件不满足时测试失败，用来暴露不符合需求的行为。'),
    ('None / 0 / b""','未指定值 / 数值零 / 空字节串：含义不同，不能只靠真假判断混为一谈。'),
]


def _run_ok(run):
    return bool(run and run['exit_code']==0 and run['passed']>0 and run['failed']==run['errors']==0 and run['cleanup_ok'])


def explain(detail):
    record=detail['record']
    title, expected, example=TASK_GUIDES.get(record['task_id'], ('任务说明','此任务暂未编写经核对的中文说明。','请对照需求与原始回复。'))
    saved=detail['saved_model_responses'];activity=detail['model_activity']
    generations=[]
    for item, run in zip(saved['generation'],activity['generation']):
        candidate=item['parsed_response']
        files=candidate.get('files') if isinstance(candidate,dict) else None
        accepted=sum(t['round']==item['round'] for t in detail['tests'])
        output=(f'回复列出{len(files)}个文件；程序最终接受并保存{accepted}个测试文件。'
                if isinstance(files,list) else '本轮没有可按标准文件列表解释的回复；不能补造生成结果。')
        if _run_ok(run['reference_run']):
            validation='正确参考版本上的测试通过，说明这一轮没有在该参考执行中报告失败。'
            mutant=run['mutant_run']
            if mutant and mutant['failure_kind']=='test_assertion':
                validation+=' 缺陷版本出现断言失败，表明测试在这次运行中触发了不符合预期的行为；完整有效检出仍须满足所有规定门槛。'
            elif _run_ok(mutant):
                validation+=' 缺陷版本也通过了本轮测试，不能据此说它发现了缺陷。'
            else:
                validation+=' 缺陷版本没有可确认通过的执行结果，应查看实际失败分类，不能把环境错误当作缺陷证据。'
        elif run['reference_run']:
            validation='正确参考版本也没有通过，这轮生成测试不能作为有效缺陷证据；需检查测试预期或运行错误。'
        else:
            validation='没有正确参考版本的对应执行记录，不能宣称这轮测试有效或已经检出缺陷。'
        generations.append({'round':item['round'],'purpose':'测试应围绕以下要求设计：'+expected,'output':output,'validation':validation})
    repairs=[]
    for item in saved['repair']:
        patch=next((p for p in detail['patches'] if p['attempt']==item['attempt']),None)
        change='修复应满足：'+expected
        if patch and record['task_id']=='dateutil_relativedelta___add':
            diff=patch['diff']
            if any(line.startswith('+') and 'if value is not None:' in line for line in diff.splitlines()):
                change='实际补丁加入if value is not None:：只有None才算没指定；0点也会被当成有效时间写入。是否保持其他行为仍须看验证和语义审查。'
        semantic='此提案没有可展示的独立语义结论，不能据模型诊断判为正确。'
        if patch:
            review=patch['review']
            if review['status']=='confirmed':
                semantic='该补丁的独立静态语义审查确认符合任务要求；它是否成为最终成功，还要看是否为最终实际应用提案及完整测试结果。'
            elif review['status']=='rejected':
                if record['task_id']=='dateutil_relativedelta___eq':
                    semantic=('这次提案漏掉默认值None的兼容行为，不能算正确。' if review['review_key'].startswith('270a0c06')
                              else '这次提案处理了None和1，却漏掉合法序号0的兼容行为；即使测试通过，也不能算修复成功。')
                else: semantic='独立语义审查拒绝该补丁，不能将测试通过当成修复正确。'
            else: semantic='独立语义审查尚不确定，不计入确认修复。'
        repairs.append({'attempt':item['attempt'],'change':change,'semantic':semantic})
    if record['group']=='B0': conclusion='这是原测试基线，没有调用模型，也没有生成或修复提案。'
    elif record['status']=='verified' and record['semantic']=='confirmed': conclusion='这条记录既通过完整技术验证，也获得最终补丁的独立语义确认。'
    elif record['status']=='verified': conclusion='虽然技术测试通过，但最终补丁未获得语义确认，不能算修复成功。'
    elif record['status']=='repair_not_reproducible' and not saved['repair']: conclusion='修复前复现门槛未通过，没有请求模型修复，没有修复补丁；这不等于模型已经尝试修复但失败。'
    else: conclusion='本条未完成经过确认的修复；请结合生成、执行和语义记录看失败发生在哪一步。'
    return {'notice':'以下中文讲解依据冻结需求、实际输出和验证记录编写，不是模型原文，也不是英文回复的逐字翻译；不作为新增正确性证据。',
        'task_title':title,'expected_behavior':expected,'example':example,
        'generation':generations,'repair':repairs,'conclusion':conclusion,
        'glossary':[{'field':k,'meaning':v} for k,v in GLOSSARY]}

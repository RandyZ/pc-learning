"""V2 instructions change output contracts, not model strength or output budgets."""
import base64
import json
from ..prompts import COMMON, PARAMETER_RULE

VERSION = "evidence-v2-prompts-3"
SOLVE = '''你正在生成完整初中数学交互讲解。只提交符合Schema的JSON。
每个小问一次提交proof、computations和scene，不单独输出网页或重复生成展示正文。
proof步骤包含稳定id；basis_ids只引用原题C编号或此前的Q编号:步骤id，不能以目标证明目标。
保留全部证明、分类、定义域与边界，优先采用可核验的初中方法；计算器不是一般几何证明器。
computations是一批待执行的受限符号计算/检查计划，不是已完成证据。关键代入、等价式与端点尽量一次列全。
注意两种协议不同：algebra_batch工具参数的variables是数组，但最终computations中的variables必须是变量名到假设的对象。
例如最终求解计划使用variables={"x":"real"}，不能使用variables=["x"]；只有题设或已论证前提支持时才可写positive/nonnegative/nonzero。
变量的positive/nonnegative/nonzero假设必须来自题设/已论证步骤；不能为了检查通过增加假设。
evaluate可直接计算常量，例如expression="1/(-1)"，此时不需要variables/substitutions；有声明变量时须全部显式代入。
simplify/evaluate可选expected；提供后会实际检查等式，错误期望值返回fail，不能删除该断言来凑通过。
equivalent/substitute_check必须填写expected；substitute_check必须有实际substitutions；solve填写equations和variables，不填expected。
例如验证(m^2+2*m)/m=m+2，可用simplify加expected="m+2"及variables={"m":"nonzero"}，前提m≠0仍须有依据。
同问引用也优先写完整形式，如Q1:q1_line，不要引用目标自身；跨问引用须明确Q编号。
每项target_step_id指本问被检查的步骤；basis_ids不得引用该步骤本身或后续步骤。
input_refs把局部变量名绑定到全局计算ID，必须同时声明depends_on；不能形成循环。
scene可以使用已有原生objects，也可以用rotate_polygon/translate_polygon/reflect_group/function_with_moving_point。
宏只机械展开，所有基点、旋转中心/方向/角度、向量、函数域和滑块区间由你明确声明，不从图形外观补题设。
宏的vertices和output_vertices逐一对应；connect=true自动补齐原像与像的闭合边；labels用于原题撇号点名。
Scene数学字段可以整项写@计算ID，只引用已完成的有限实数标量；不支持在字符串中拼接@表达式。
required_visible及step_links明确列出讲解所需的点线，函数没有可见点时必须提供manual的具名view。
所有小问完整保留；工具仅在不能通过最终计划表达的必要取证时使用，不重复请求已得到的计算。
'''
PATCH = '''只输出ChangeSet，不重新生成整题。expected_candidate_revision必须与输入一致。
只能修改allowed_fields授权的小问/字段。图形修订不能修改proof或computations；数学修订不能改原题或无授权的scene。
修改字段提供完整的新值；未改字段省略。步骤id尽量稳定，内容变更仍产生新版本并重新审核。
修正错误的计算计划须说明原错误原因；不得删除真实未决问题来凑通过。
simplify/evaluate中的expected也是必须保留并重验的断言；常量evaluate不要求变量替换。
'''


FIELD_CORRECTION = '''只返回字段补丁对象：base_sha256和replacements，不返回整份Draft或ChangeSet。
replacements的键必须与列出的路径完全一致；每个值只修正对应variables的结构，变量名保持不变。
根据原题及proof_context明确变量假设，不能为通过检查而假设正数或非零；无法判断时该路径返回null，程序将保留未决并停止。
未列出的证明、公式、计算目标、expected、依赖、画板及控件由服务器逐字保留，禁止改动。
不要重复生成题解，不调用工具；提交一个完整JSON对象，不附带文字或多余根括号。
'''


def messages(request, directory, payload, schema, *, patch=False, field_only=False):
    task = FIELD_CORRECTION if field_only else PATCH if patch else SOLVE
    if not field_only:
        task += "\n" + PARAMETER_RULE
    task += "\n输出Schema：" + json.dumps(schema, ensure_ascii=False)
    content = [{"type": "text", "text": json.dumps({"question_text": request["text"],
        "image_roles": [{"id": i["id"], "role": i["role"]} for i in request["images"]],
        "context": payload}, ensure_ascii=False)}]
    for image in request["images"]:
        path = (directory / image["file"]).resolve()
        if not path.is_relative_to(directory.resolve()):
            raise ValueError("image path outside task")
        content.extend([{"type": "text", "text": f"图片{image['id']}，角色：{image['role']}（参考答案不是题设）"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()}}])
    from ..solve_views import solve_views
    content.extend(solve_views(request, directory))
    return [{"role": "system", "content": COMMON + task}, {"role": "user", "content": content}]

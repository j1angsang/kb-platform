# 部门名唯一真相：权限过滤下推 SQL WHERE，多处各抄一份就会漂移
# 注册不接受部门，新用户一律 UNASSIGNED；分配部门只在 routers/admin.py 按下面这个列表校验

# 管理员可分配的部门
ALLOWED_DEPARTMENTS = ("人事部", "财务部", "技术部", "行政部", "销售部")

# 部门归属是授权数据，只能服务端写，注册者不能自选
UNASSIGNED = "未分配"

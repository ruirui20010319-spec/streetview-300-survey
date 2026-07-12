import pandas as pd
import os

# 1. 填入你的阿里云 OSS 基础访问域名（记得以 https:// 开头，末尾不要带斜杠 /）
# 请把下面这个示例网址，改成你真实的 OSS 访问域名！
OSS_BASE_URL = "your-bucket-name.oss-cn-hangzhou.aliyuncs.com"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSIGNMENT_PATH = os.path.join(BASE_DIR, 'tables', 'questionnaire_pair_assignment_150x30.xlsx')

def get_survey_questions(slot_id):
    print(f"【DEBUG】当前系统正在物理寻找的文件路径是:\n{ASSIGNMENT_PATH}")
    
    if not os.path.exists(ASSIGNMENT_PATH):
        print(f"【错误】在上述路径下未找到该 Excel 表，请确保文件已放入 tables 文件夹中。")
        return []

    try:
        df = pd.read_excel(ASSIGNMENT_PATH)
        df.columns = df.columns.str.strip()
        
        val = int(str(slot_id).strip())
        target = f"P_SLOT_{val:03d}"
        
        print(f"【DEBUG】正在从表格中筛选槽位: '{target}' 的题目...")
        
        user_data = df[df['participant_slot'].astype(str).str.strip() == target]
        
        if user_data.empty:
            print(f"【警告】在表格中未找到槽位 '{target}' 的任何数据")
            return []

        if 'order_in_participant' in user_data.columns:
            user_data = user_data.sort_values(by='order_in_participant')

        questions = []
        for idx, (_, row) in enumerate(user_data.iterrows(), start=1):
            # 清理文件名逻辑
            left_f = str(row.get('left_image_filename', '')).strip()
            right_f = str(row.get('right_image_filename', '')).strip()
            
            # 如果表格里的名字自带了 "images/"，我们先把它去掉，防止后面拼重了
            left_f = left_f.replace('images/', '')
            right_f = right_f.replace('images/', '')
            
            # 核心改动：不再走服务器本地 static，直接拼接成 OSS 上的绝对网络图片地址
            # 假设你在 OSS 上的结构是把图片放在了一个名为 images 的文件夹里
            left_url = f"{OSS_BASE_URL}/images/{left_f}"
            right_url = f"{OSS_BASE_URL}/images/{right_f}"
            
            questions.append({
                'order': idx,
                'pair_id': str(row.get('pair_id', '')),
                'left_qid': str(row.get('left_qid', '')),
                'right_qid': str(row.get('right_qid', '')),
                'left_image_id': str(row.get('left_image_id', '')),
                'right_image_id': str(row.get('right_image_id', '')),
                'left_img_url': left_url,
                'right_img_url': right_url
            })
            
        print(f"【DEBUG】成功为槽位 {target} 载入 {len(questions)} 道题。图片全部指向 OSS 云存储。")
        return questions

    except Exception as e:
        print(f"【错误】data_loader 读取异常: {e}")
        return []
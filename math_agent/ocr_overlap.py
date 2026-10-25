"""Run the same private OCR beside visual reading; join before any review/publish."""
import copy
import time
from concurrent.futures import ThreadPoolExecutor,TimeoutError


class PendingOCR:
    def __init__(self,settings,request,directory,audit,deadline,recognize):
        self.deadline=None if getattr(settings,"diagnostic_mode",False) else deadline
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='math-ocr')
        self.audit=audit
        self.future=self.pool.submit(recognize,settings,copy.deepcopy(request),directory,audit)
        self.joined=False
        audit('ocr_overlap',{'status':'started','detail':'OCR与原图模型读取并行；结果必须在复核和发布前取得，失败不得跳过'})

    def result(self):
        try:value=self.future.result(timeout=None if self.deadline is None else max(0,self.deadline-time.monotonic()))
        except TimeoutError:raise RuntimeError('整题截止前OCR未完成，不能复核或发布；未自动重发') from None
        if not self.joined:
            self.joined=True
            self.audit('ocr_overlap',{'status':'joined','pages':len(value),
                'detail':'原始OCR证据已合并，独立复核必须与原图、题设逐条对照；OCR不是题设真值'})
            self.pool.shutdown(wait=False)
        return value

    def close(self):
        # Started HTTP work cannot be cancelled by Future.cancel. The worker's
        # process supervisor still owns cleanup/cancellation; no request is resubmitted.
        self.pool.shutdown(wait=False,cancel_futures=True)

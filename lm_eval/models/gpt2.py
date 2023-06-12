import torch
import transformers
from typing import Optional
from lm_eval.base import BaseLM

class HFLM(BaseLM):
    def __init__(
        self,
        device="cuda",
        pretrained="gpt2",
        revision="main",
        low_cpu_mem_usage=None,
        subfolder=None,
        tokenizer=None,
        batch_size=1,
        load_in_8bit: Optional[bool] = False,
        trust_remote_code: Optional[bool] = False,
    ):
        super().__init__()

        assert isinstance(device, str)
        assert isinstance(pretrained, str)
        assert isinstance(batch_size, (int,str))

        device_list = set(["cuda", "cpu"] + [f'cuda:{i}' for i in range(torch.cuda.device_count())])
        if device and device in device_list:
            self._device = torch.device(device)
            print(f"Using device '{device}'")
        else:
            print("Device not specified")
            print(f"Cuda Available? {torch.cuda.is_available()}")
            self._device = (
                torch.device("cuda")
                if torch.cuda.is_available()
                else torch.device("cpu")
            )

        # TODO: update this to be less of a hack once subfolder is fixed in HF
        revision = revision + ("/" + subfolder if subfolder is not None else "")

        self.gpt2 = transformers.AutoModelForCausalLM.from_pretrained(
            pretrained,
            load_in_8bit=load_in_8bit,
            low_cpu_mem_usage=low_cpu_mem_usage,
            revision=revision,
            trust_remote_code=trust_remote_code,
        ).to(self.device)
        self.gpt2.eval()

        # AutoTokenizer seems loading very slow
        self.tokenizer = transformers.AutoTokenizer.from_pretrained(
            pretrained if tokenizer is None else tokenizer,
            revision=revision,
            trust_remote_code=trust_remote_code,
        )

        self.vocab_size = self.tokenizer.vocab_size

        if isinstance(
            self.tokenizer, (transformers.GPT2Tokenizer, transformers.GPT2TokenizerFast)
        ):
            assert self.tokenizer.encode("hello\n\nhello") == [
                31373,
                198,
                198,
                31373,
            ], self.tokenizer.encode("hello\n\nhello")

        # setup for automatic batch size detection
        if batch_size == 'auto': 
            self.batch_size_per_gpu = batch_size
        else:
            self.batch_size_per_gpu = int(batch_size) 

    @property
    def eot_token_id(self):
        # we use EOT because end of *text* is more accurate for what we're doing than end of *sentence*
        return self.tokenizer.eos_token_id

    @property
    def max_length(self):
        try:
            return self.gpt2.config.n_ctx
        except AttributeError:
            # gptneoconfig doesn't have n_ctx apparently
            return self.gpt2.config.max_position_embeddings

    @property
    def max_gen_toks(self):
        return 256

    @property
    def batch_size(self):
        # TODO: fix multi-gpu
        return self.batch_size_per_gpu  # * gpus

    @property
    def device(self):
        # TODO: fix multi-gpu
        return self._device

    def tok_encode(self, string: str):
        return self.tokenizer.encode(string, add_special_tokens=False)

    def tok_decode(self, tokens):
        return self.tokenizer.decode(tokens)

    def _model_call(self, inps):
        """
        inps: a torch tensor of shape [batch, sequence]
        the size of sequence may vary from call to call

        returns: a torch tensor of shape [batch, sequence, vocab] with the
        logits returned from the model
        """
        with torch.no_grad():
            return self.gpt2(inps)[0]

    def _model_generate(self, context, max_length, eos_token_id):
        generation_kwargs = {'do_sample': False, 'max_length': max_length}
        if eos_token_id is not None:
            generation_kwargs['eos_token_id'] = eos_token_id
            generation_kwargs['pad_token_id'] = eos_token_id # setting eos_token_id as pad token
        return self.gpt2.generate(context, **generation_kwargs)


# for backwards compatibility
GPT2LM = HFLM


class LlamaCPPLM(BaseLM):
    def __init__(
        self,
        device="cuda",
        pretrained="gpt2",
        revision="main",
        low_cpu_mem_usage=None,
        subfolder=None,
        tokenizer=None,
        batch_size=1,
        load_in_8bit: Optional[bool] = False,
        trust_remote_code: Optional[bool] = False,
    ):
        super().__init__()

        assert isinstance(pretrained, str)
        assert isinstance(batch_size, (int,str))

        import os
        import multiprocessing
        # TODO: config n_ctx and n_batch
        if "neox" in pretrained:
            from gptneox_cpp import Gptneox
            self.model = Gptneox(pretrained, use_mmap=False, logits_all=True,
                                 n_ctx=2048,  # n_batch=2048,
                                 n_threads=int(os.environ.get("OMP_NUM_THREADS", multiprocessing.cpu_count()/2)))
        else:
            from llama_cpp import Llama
            self.model = Llama(model_path=pretrained, logits_all=True,
                               n_ctx=2048,  # n_batch=2048,
                               n_threads=int(os.environ.get("OMP_NUM_THREADS", multiprocessing.cpu_count()/2)))
        # gptneox int4 tokenizer differs from huggingface tokenizer, which will impact accuracy
        # TODO: remove hardcode
        tokenizer_path = "/home/kai/llm/gptneox-7b-redpajama-bf16"
        self.tokenizer = transformers.AutoTokenizer.from_pretrained(
            tokenizer_path if tokenizer is None else tokenizer,
            revision=revision,
            trust_remote_code=trust_remote_code,
        )

        # setup for automatic batch size detection
        if batch_size == 'auto':
            self.batch_size_per_gpu = batch_size
        else:
            self.batch_size_per_gpu = int(batch_size)

    @property
    def eot_token_id(self):
        # we use EOT because end of *text* is more accurate for what we're doing than end of *sentence*
        return self.model.token_eos()

    @property
    def max_length(self):
        return 2048  # TODO: how to get this from config

    @property
    def max_gen_toks(self):
        return 256

    @property
    def batch_size(self):
        # TODO: fix multi-gpu
        return self.batch_size_per_gpu  # * gpus

    @property
    def device(self):
        # TODO: fix multi-gpu
        return torch.device("cpu")

    def tok_encode(self, string: str):
        # tokens = self.model.tokenize(string.encode("utf-8"))
        # return tokens[1:]  # Remove the special token at the very beginning
        return self.tokenizer.encode(string, add_special_tokens=False)

    def tok_decode(self, tokens):
        return self.model.detokenize(tokens)

    def _model_call(self, inps):
        """
        inps: a torch tensor of shape [batch, sequence]
        the size of sequence may vary from call to call

        returns: a torch tensor of shape [batch, sequence, vocab] with the
        logits returned from the model
        """
        # import time
        # start = time.time()
        self.model.reset()
        self.model.eval(inps.tolist()[0])
        if hasattr(self.model, "eval_logits"):
            res = self.model.eval_logits
        else:  # Old version
            res = self.model.all_logits
        # end = time.time()
        # print("Eval time: {}s".format(end - start))
        return torch.Tensor([res])

    def _model_generate(self, context, max_length, eos_token_id):
        return self.model(context, max_tokens=max_length, stop=["Q:", "\n"], echo=True)


class BloomzCPPLM(BaseLM):
    def __init__(
        self,
        device="cuda",
        pretrained="gpt2",
        revision="main",
        low_cpu_mem_usage=None,
        subfolder=None,
        tokenizer=None,
        batch_size=1,
        load_in_8bit: Optional[bool] = False,
        trust_remote_code: Optional[bool] = False,
    ):
        super().__init__()

        assert isinstance(pretrained, str)
        assert isinstance(batch_size, (int,str))

        import os
        import multiprocessing
        # TODO: config n_ctx and n_batch
        if "neox" in pretrained:
            from gptneox_cpp import Gptneox
            self.model = Gptneox(pretrained, use_mmap=False, logits_all=True,
                                 n_ctx=2048,  # n_batch=2048,
                                 n_threads=int(os.environ.get("OMP_NUM_THREADS", multiprocessing.cpu_count()/2)))
        else:
            from bloom_cpp import Bloom
            self.model = Bloom(model_path=pretrained, logits_all=True,
                               n_ctx=2048,  # n_batch=2048,
                               n_threads=int(os.environ.get("OMP_NUM_THREADS", multiprocessing.cpu_count()/2)))
        # gptneox int4 tokenizer differs from huggingface tokenizer, which will impact accuracy

        # setup for automatic batch size detection
        if batch_size == 'auto':
            self.batch_size_per_gpu = batch_size
        else:
            self.batch_size_per_gpu = int(batch_size)

    @property
    def eot_token_id(self):
        # we use EOT because end of *text* is more accurate for what we're doing than end of *sentence*
        return self.model.token_eos()

    @property
    def max_length(self):
        return 2048  # TODO: how to get this from config

    @property
    def max_gen_toks(self):
        return 256

    @property
    def batch_size(self):
        # TODO: fix multi-gpu
        return self.batch_size_per_gpu  # * gpus

    @property
    def device(self):
        # TODO: fix multi-gpu
        return torch.device("cpu")

    def tok_encode(self, string: str):
        # tokens = self.model.tokenize(string.encode("utf-8"))
        # return tokens[1:]  # Remove the special token at the very beginning
        # return self.tokenizer.encode(string, add_special_tokens=False)
        tokens = self.model.tokenize(string.encode("utf-8"))
        return tokens


    def tok_decode(self, tokens):
        return self.model.detokenize(tokens)

    def _model_call(self, inps):
        """
        inps: a torch tensor of shape [batch, sequence]
        the size of sequence may vary from call to call

        returns: a torch tensor of shape [batch, sequence, vocab] with the
        logits returned from the model
        """
        # import time
        # start = time.time()
        res = self.model.eval(inps.tolist()[0])
        # if hasattr(self.model, "eval_logits"):
        #     res = self.model.eval_logits
        # else:  # Old version
        #     res = self.model.all_logits
        # # end = time.time()
        # # print("Eval time: {}s".format(end - start))
        return torch.Tensor([res])

    def _model_generate(self, context, max_length, eos_token_id):
        return self.model(context, max_tokens=max_length, stop=["Q:", "\n"], echo=True)

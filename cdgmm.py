from skcuda import cublas
import torch
from collections import defaultdict, namedtuple
from torch.autograd import Function
from torch.autograd import Variable
from pynvrtc.compiler import Program
from cupy.cuda.function import Module
from cupy.cuda import device


kernel = """
extern "C"
__global__ void swap(float2 *x, int total)
{
   int tx = blockIdx.x * blockDim.x + threadIdx.x;
   if(tx >= total)
      return;

   float2 v = x[tx];
   x[tx] = make_float2(v.y, v.x);
}
"""

CUDA_NUM_THREADS = 1024


def GET_BLOCKS(N, K=CUDA_NUM_THREADS):
    return (N + K - 1) // K


Stream = namedtuple('Stream', ['ptr'])

modules = defaultdict(lambda: None)


def get_compute_arch(t):
    return 'compute_%s' % device.Device().compute_capability


def compile(input):
    if modules[input.get_device()] is None:
        print 'compiling for dev', input.get_device()
        program = Program(kernel, 'ncrelu.cu')
        ptx = program.compile(['-arch=' + get_compute_arch(input)])

        module = Module()
        module.load(bytes(ptx.encode()))
        modules[input.get_device()] = module
    else:
        module = modules[input.get_device()]
    return module


def swap(x):
    assert x.size(-1) == 2
    total = x.numel() // 2
    module = compile(x)
    f = module.get_function('swap')
    f(args=[x.data_ptr(), total],
      block=(CUDA_NUM_THREADS,1,1),
      grid=(GET_BLOCKS(total),1,1),
      stream=Stream(ptr=torch.cuda.current_stream().cuda_stream))


def cublas_cdgmm(A, x, out=None):
    if out is not None:
        assert out.is_contiguous() and out.size() == A.size()
    else:
        out = A.new(A.size())
    assert x.dim() == 2 and x.size(-1) == 2 and A.size(-1) == 2
    assert A.dim() == 3
    assert x.size(0) == A.size(1) or x.size(0) == A.size(0)
    assert A.type() == x.type() == out.type()
    assert A.is_contiguous()

    if not isinstance(A, (torch.cuda.FloatTensor, torch.cuda.DoubleTensor)):
        raise NotImplementedError
    else:
        m, n = A.size(1), A.size(0)
        if x.size(0) == A.size(1):
            mode = 'l'
        elif x.size(0) == A.size(0):
            mode = 'r'
        lda, ldc = m, m
        incx = 1
        handle = torch.cuda.current_blas_handle()
        stream = torch.cuda.current_stream()._as_parameter_
        cublas.cublasSetStream(handle, stream)
        args = [handle, mode, m, n, A.data_ptr(), lda, x.data_ptr(), incx, out.data_ptr(), ldc]
        if isinstance(A, torch.cuda.FloatTensor):
            cublas.cublasCdgmm(*args)
        elif isinstance(A, torch.cuda.DoubleTensor):
            cublas.cublasZdgmm(*args)
        return out


class CDGMM(Function):
    def forward(self, input, x):
        self.save_for_backward(input, x)
        return cublas_cdgmm(input, x)

    def backward(self, grad_output):
        input, x = self.saved_tensors
        grad_input = grad_x = None
        if self.needs_input_grad[0]:
            grad_input = cublas_cdgmm(grad_output.contiguous(), x)
            swap(grad_input)
            assert grad_input.size() == input.size()
        if self.needs_input_grad[1]:
            raise NotImplementedError
            # dim = 0 if x.size(0) == input.size(1) else 1
            # grad_x = (grad_output * input).sum(dim).squeeze(dim)
            # assert grad_x.size() == x.size()
        return grad_input, grad_x


def cdgmm(input, x):
    return CDGMM()(input, x)
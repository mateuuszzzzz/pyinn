from string import Template
from pynvrtc.compiler import Program
import torch
from cupy.cuda.function import Module
from .utils import Stream, get_compute_arch, Dtype

CUDA_NUM_THREADS = 1024


def GET_BLOCKS(N, K=CUDA_NUM_THREADS):
    return (N + K - 1) // K


# k = i_n * 2CHW + i_c * HW + i_h * W + i_w
# i_n * 2CHW + (i_c + C) * HW + i_h * W + i_w =
# k + chw


kernels = '''
extern "C"
__global__ void ncrelu_forward(${Dtype} *dst, unsigned char* mask, const ${Dtype} *src, int chw, int total)
{
   int tx = blockIdx.x * blockDim.x + threadIdx.x;
   if(tx >= total)
      return;

   ${Dtype} v = src[tx];
   unsigned char flag = v >= 0;
   mask[tx] = flag;
   dst[tx + tx / chw * chw] = flag ? v : 0.f;
   dst[tx + tx / chw * chw + chw] = flag ? 0.f : v;
}

extern "C"
__global__ void ncrelu_backward(${Dtype} *grad_input, const unsigned char *mask, const ${Dtype} *grad_output,
                                int chw, int total)
{
   int tx = blockIdx.x * blockDim.x + threadIdx.x;
   if(tx >= total)
      return;

   grad_output += tx + tx / chw * chw;
   bool flag = mask[tx];
   grad_input[tx] = flag ? grad_output[0] : grad_output[chw];
}
'''

fwd_modules = {}
bwd_modules = {}


def compile(modules, input):
    k = (input.get_device(), type(input))
    if k not in modules:
        print 'compiling for dev', input.get_device()
        src = Template(kernels).substitute(Dtype=Dtype(input))
        program = Program(src, 'ncrelu.cu')
        ptx = program.compile(['-arch=' + get_compute_arch(input)])

        module = Module()
        module.load(bytes(ptx.encode()))
        modules[k] = module
    else:
        module = modules[k]
    return module


def ncrelu_forward(input):
    module = compile(fwd_modules, input)

    assert input.dim() == 4 and input.is_contiguous()
    n, c, h, w = input.size()

    output = input.new(n, 2 * c, h, w)
    mask = torch.cuda.ByteTensor(input.size())

    f = module.get_function('ncrelu_forward')

    f(args=[output.data_ptr(), mask.data_ptr(), input.data_ptr(), c*h*w, input.numel()],
      block=(CUDA_NUM_THREADS,1,1),
      grid=(GET_BLOCKS(input.numel()),1,1),
      stream=Stream(ptr=torch.cuda.current_stream().cuda_stream))
    return output, mask


def ncrelu_backward(grad_output, mask):
    module = compile(bwd_modules, grad_output)
    assert grad_output.get_device() == mask.get_device()
    assert grad_output.is_contiguous()
    n, c, h, w = mask.size()
    grad_input = grad_output.new(mask.size())

    f = module.get_function('ncrelu_backward')
    f(args=[grad_input.data_ptr(), mask.data_ptr(), grad_output.data_ptr(), c*h*w, mask.numel()],
      block=(CUDA_NUM_THREADS,1,1),
      grid=(GET_BLOCKS(mask.numel()),1,1),
      stream=Stream(ptr=torch.cuda.current_stream().cuda_stream))
    return grad_input


class NCRELU(torch.autograd.Function):

    def forward(self, input):
        output, self.mask = ncrelu_forward(input)
        return output

    def backward(self, grad_output):
        return ncrelu_backward(grad_output, self.mask)


def ncrelu(input):
    """ Applies NCReLU (negative concatenated ReLU) nonlinearity.

    Does `torch.cat([x.clamp(min=0), x.clamp(max=0)], dim=1)` in
    a single fused op.

    Args:
        input: 4D tensor
    """
    if not input.is_cuda:
        return torch.cat([input.clamp(min=0), input.clamp(max=0)], dim=1)
    else:
        return NCRELU()(input)